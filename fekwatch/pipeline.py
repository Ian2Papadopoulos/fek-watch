"""Daily monitoring and amendment-history backfill."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .amendments import ActAnalysis, Hit, analyze, hits_for
from .citations import ACTION_LABELS_EL, Locator, find_mentions
from .config import Config, WatchedAct
from .et_api import EtClient, FekMeta, group_code, GROUP_BY_NAME
from .normalize import fold
from .pdftext import extract_pdf
from .store import Store, now_iso

log = logging.getLogger(__name__)
try:
    ATHENS = ZoneInfo("Europe/Athens")
except Exception:  # noqa: BLE001  (Windows without the tzdata package)
    ATHENS = None


def _athens_now() -> datetime:
    if ATHENS is not None:
        return datetime.now(ATHENS)
    # EU rule: UTC+3 from the last Sunday of March to the last Sunday of October (01:00 UTC), else UTC+2
    from datetime import timezone
    now = datetime.now(timezone.utc)

    def last_sunday(month):
        d = datetime(now.year, month, 31 if month in (3, 10) else 30, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    summer = last_sunday(3) <= now < last_sunday(10)
    return now + timedelta(hours=3 if summer else 2)
PARSER_VERSION = 1


def today_athens() -> date:
    return _athens_now().date()


# --------------------------------------------------------------------------- analysis

def analyze_meta(meta: FekMeta, client: EtClient, store: Store, cfg: Config) -> ActAnalysis:
    return analyze(get_doc(meta, client, store, cfg), meta.year, cfg.aliases(), meta.issue_date)


def get_doc(meta: FekMeta, client: EtClient, store: Store, cfg: Config | None = None):
    """Extracted text of an issue (cached). PDFs are deleted after extraction unless keep_pdfs."""
    doc = store.get_extracted(meta.et_id)
    if doc is None:
        pdf = client.download(meta, store.cache)
        doc = extract_pdf(pdf)
        store.put_extracted(meta.et_id, doc)
        if not (cfg and cfg.settings.keep_pdfs):
            pdf.unlink(missing_ok=True)
    return doc


def _rid(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]


def _source(meta: FekMeta, act: str | None, act_title: str, page: int | None) -> dict:
    return {
        "et_id": meta.et_id, "label": meta.label, "group": meta.group, "number": meta.number, "year": meta.year,
        "issue_date": meta.issue_date, "release_date": meta.release_date, "act": act, "act_title": act_title,
        "page": page, "pdf": meta.pdf + (f"#page={page}" if page else ""), "et_page": meta.et_page,
    }


def summarize(actions: list[str], locator: dict) -> str:
    loc = Locator(**locator).describe() if locator else ""
    acts = ", ".join(ACTION_LABELS_EL.get(a, a) for a in actions) if actions else ""
    return f"{acts} — {loc}" if acts else loc


def _snippet_around(text: str, w: WatchedAct, width: int = 220) -> str:
    ms = find_mentions(text, w.kind, w.number, w.year, w.aliases)
    if not ms:
        return text[: 2 * width]
    s, e, _ = ms[0]
    a, b = max(0, s - width), min(len(text), e + width)
    return ("…" if a else "") + text[a:b] + ("…" if b < len(text) else "")


def make_record(meta: FekMeta, an: ActAnalysis, hit: Hit, w: WatchedAct) -> dict:
    p = hit.provision
    rec = {
        "id": _rid(hit.watched, meta.et_id, p.article, p.label, hit.kind),
        "watched": hit.watched,
        "kind": hit.kind,
        "confidence": round(hit.confidence, 2),
        "source": _source(meta, p.act, an.act_title, p.page),
        "article": p.article,
        "article_title": p.article_title,
        "par": p.label,
        "locator": p.locator,
        "articles": p.articles,
        "actions": p.actions,
        "summary": summarize(p.actions, p.locator) if hit.kind == "amendment" else "",
        "target_how": p.target_how,
        "whole_act": p.whole_act,
        "detected_at": now_iso(),
        "parser_version": PARSER_VERSION,
    }
    if hit.kind == "amendment":
        rec["instruction"] = p.instruction
        rec["payloads"] = [{"id": pid, "page": an.payloads[pid].page, "text": an.payloads[pid].text}
                           for pid in p.payload_ids if pid in an.payloads]
    elif hit.kind == "quoted":
        rendered = an.render(p)
        rec["instruction"] = an.render(p, max_payload=160)[:1200]
        rec["snippet"] = _snippet_around(rendered, w)
    else:
        rendered = an.render(p, max_payload=300)
        rec["instruction"] = rendered[:1500]
        rec["snippet"] = _snippet_around(rendered, w)
    return rec


def add_record(law: dict, rec: dict) -> bool:
    """Insert/update a record. Returns True when it is new."""
    old = law["records"].get(rec["id"])
    if old:
        rec["detected_at"] = old.get("detected_at", rec["detected_at"])
        for k in ("review",):  # keep human review notes
            if k in old:
                rec[k] = old[k]
        law["records"][rec["id"]] = rec
        return False
    law["records"][rec["id"]] = rec
    return True


# --------------------------------------------------------------------------- mentions in other issues

def _mention_records(meta: FekMeta, w: WatchedAct, client: EtClient, store: Store) -> list[dict]:
    """One record per (watched act, issue) for decisions etc. (Β', Δ', ...) that mention it."""
    doc = get_doc(meta, client, store)
    heading, title, snippets, first_page, conf = "", "", [], None, 0.0
    for para in doc.paragraphs:
        if para.bold:
            t = re.sub(r"^(?:ΔΙΑΤΑΓΜΑΤΑ|ΑΠΟΦΑΣΕΙΣ|ΑΝΑΚΟΙΝΩΣΕΙΣ)\s+", "", para.text)
            if not re.match(r"^(?:ΠΕΡΙΕΧΟΜΕΝΑ|ΑΠΟΦΑΣΕΙΣ|ΔΙΑΤΑΓΜΑΤΑ|ΣΥΜΠΛΗΡΩΝΟΝΤΑΙ|ΑΝΑΚΟΙΝΩΣΕΙΣ)\b", t):
                heading = t
            continue
        if para.kind == "toc":
            continue
        ms = find_mentions(para.text, w.kind, w.number, w.year, w.aliases)
        if not ms or max(c for *_, c in ms) < 0.8:
            continue
        conf = max(conf, max(c for *_, c in ms))
        if first_page is None:
            first_page, title = para.page, heading
        if len(snippets) < 3:
            snippets.append({"page": para.page, "text": _snippet_around(para.text, w, 180)})
    if not snippets:
        return []
    return [{
        "id": _rid(w.key, meta.et_id, "mention"),
        "watched": w.key,
        "kind": "mention",
        "confidence": conf,
        "source": _source(meta, None, title[:300], first_page),
        "article": "", "article_title": title[:300], "par": "",
        "locator": {}, "actions": [], "summary": "", "target_how": "",
        "snippet": " … ".join(f"[σελ. {s_['page']}] {s_['text']}" for s_ in snippets),
        "detected_at": now_iso(), "parser_version": PARSER_VERSION,
    }]


# --------------------------------------------------------------------------- daily

def run_daily(cfg: Config, store: Store, client: EtClient, until: date | None = None,
              since: date | None = None) -> dict:
    until = until or today_athens()
    since = since or (until - timedelta(days=cfg.settings.lookback_days))
    st = store.state()
    st.setdefault("processed", {})
    st.setdefault("mentions_seen", {})
    laws = {w.key: store.load_law(w.slug, w.key) for w in cfg.laws}
    new: list[dict] = []
    checked: list[str] = []
    errors: list[str] = []

    groups = [group_code(g) for g in cfg.settings.analyze_groups]
    metas = client.search(groups=groups, released=(since.isoformat(), until.isoformat()))
    metas.sort(key=lambda m: (m.release_date or "", m.group, m.number))
    for meta in metas:
        if str(meta.et_id) in st["processed"]:
            continue
        try:
            an = analyze_meta(meta, client, store, cfg)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed to analyse %s", meta.label)
            errors.append(f"{meta.label}: {exc}")
            continue
        checked.append(meta.label)
        hits = hits_for(an, cfg.watched_map())
        n_new = 0
        for hit in hits:
            w = next(x for x in cfg.laws if x.key == hit.watched)
            if hit.kind in ("reference", "quoted") and hit.confidence < cfg.settings.min_reference_confidence:
                continue
            rec = make_record(meta, an, hit, w)
            if add_record(laws[w.key], rec):
                new.append(rec)
                n_new += 1
        st["processed"][str(meta.et_id)] = {
            "label": meta.label, "release_date": meta.release_date, "act": an.act,
            "title": an.act_title[:200], "provisions": len(an.provisions), "hits": n_new, "at": now_iso(),
        }

    mention_groups = [group_code(g) for g in cfg.settings.mention_groups]
    if mention_groups:
        for w in cfg.laws:
            try:
                groups_w = sorted(set(mention_groups) | ({2} if w.is_decision else set()))
                res = client.search(groups=groups_w, text=w.search_text,
                                    released=(since.isoformat(), until.isoformat()))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"mention search {w.key}: {exc}")
                continue
            for meta in res:
                k = f"{w.key}|{meta.et_id}"
                if k in st["mentions_seen"]:
                    continue
                try:
                    if w.is_decision and meta.group == 2:
                        # another decision may amend the watched decision: analyse it like a law
                        an = analyze_meta(meta, client, store, cfg)
                        recs = [make_record(meta, an, h, w) for h in hits_for(an, {w.key: w.as_match()})
                                if h.kind in ("amendment", "derogation") or h.confidence >= cfg.settings.min_reference_confidence]
                    else:
                        recs = _mention_records(meta, w, client, store)
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{meta.label}: {exc}")
                    continue
                st["mentions_seen"][k] = now_iso()
                for rec in recs:
                    if add_record(laws[w.key], rec):
                        new.append(rec)

    for w in cfg.laws:
        laws[w.key]["name"] = w.name
        store.save_law(w.slug, laws[w.key])
    st["last_run"] = now_iso()
    st["last_window"] = [since.isoformat(), until.isoformat()]
    store.save_state(st)
    summary = {
        "at": now_iso(), "window": [since.isoformat(), until.isoformat()], "checked": checked,
        "new": [{"id": r["id"], "watched": r["watched"], "kind": r["kind"], "fek": r["source"]["label"]} for r in new],
        "errors": errors,
    }
    store.append_run(summary)
    summary["new_records"] = new
    return summary


# --------------------------------------------------------------------------- history

def _meta_from_label(et_id: int, label: str, client: EtClient) -> FekMeta | None:
    m = re.match(r"^(.+?)\s+(\d+)/(\d{4})$", label.strip())
    if not m:
        return None
    try:
        g = group_code(m.group(1))
    except ValueError:
        return None
    info = {}
    try:
        info = client.document(et_id)
    except Exception:  # noqa: BLE001
        pass
    from .et_api import _parse_date
    return FekMeta(et_id, g, int(m.group(2)), int(m.group(3)), _parse_date(info.get("IssueDate")),
                   _parse_date(info.get("PublicationDate")), int(info.get("Pages") or 0))


def _fek_dict(m: FekMeta, description: str = "") -> dict:
    d = m.to_dict()
    d["description"] = description
    return {k: d.get(k) for k in ("et_id", "label", "issue_date", "release_date", "pages", "pdf", "et_page",
                                  "description", "group", "number", "year")}


def resolve_act_fek(w: WatchedAct, client: EtClient) -> dict | None:
    if w.fek:  # given in watchlist.toml, e.g. "Β 3985/2023"
        mm = re.match(r"^\s*(.+?)\s*(\d+)\s*/\s*(\d{4})\s*$", w.fek)
        if mm:
            g, num, yr = group_code(mm.group(1)), int(mm.group(2)), int(mm.group(3))
            hit = [x for x in client.search(years=[yr], groups=[g], number=num) if x.number == num]
            if hit:
                return _fek_dict(hit[0], w.name)
    if w.is_decision:
        # the issue that publishes it: its snippet starts with "αποφασεισ αριθμ. <protocol>"
        res = client.search(years=[w.year], groups=[2], text=w.search_text)
        core = fold(w.search_text)
        for m in sorted(res, key=lambda m: m.issue_date or ""):
            if re.search(r"αριθμ\.?\s*\S*" + re.escape(core), fold(m.snippet or "")):
                return _fek_dict(m, w.name)
        return None
    found = client.find_act(w.kind, w.number, w.year)
    found = [f for f in found if f.get("group") == 1] or found
    if not found:
        return None
    f = found[0]
    return {k: f.get(k) for k in ("et_id", "label", "issue_date", "release_date", "pages", "pdf", "et_page",
                                  "description", "group", "number", "year")}


_KIND_WORDS = {"ν.": r"ν\.?|νομ\w*", "π.δ.": r"π\.?\s?δ\.?|διαταγματ\w*", "ν.δ.": r"ν\.?\s?δ\.?|διαταγματ\w*",
               "α.ν.": r"α\.?\s?ν\.?|νομ\w*", "β.δ.": r"β\.?\s?δ\.?|διαταγματ\w*", "π.ν.π.": r"π\.?\s?ν\.?\s?π\.?"}


def _snippet_may_cite(m: FekMeta, w: WatchedAct) -> bool:
    """Cheap pre-filter when a number is too common in ET's index (e.g. '41/2018' -> ~1,000 issues):
    keep an issue if ET's snippet shows the number as this act ("π.δ. 41/2018"), or cannot tell
    (no snippet, or the snippet starts at the number with no context)."""
    f = fold(m.snippet or "")
    if not f:
        return True
    num = r"(?<![\d/])" + re.escape(w.search_text).replace("/", r"\s?/\s?") + r"(?![\d])"
    i = re.search(num, f)
    if i is None:
        return False  # ET matched the digits separately ("41 ... 2018"): not a citation of this act
    if i.start() < 15:
        return True
    words = _KIND_WORDS.get(w.kind, "")
    pat = r"(?:" + words + r")\s*\(?\s*" + num if words else num
    if re.search(pat, f):
        return True
    return any(fold(a) in f for a in w.aliases)


def run_history(cfg: Config, store: Store, client: EtClient, w: WatchedAct,
                progress=None, until: date | None = None, rebuild: bool = False) -> dict:
    """Find every FEK Α' that amends `w` since its publication.

    rebuild=True drops previously detected records (keeping any with a human review note)
    and re-analyses everything from the text cache, e.g. after a parser improvement."""
    until = until or today_athens()
    law = store.load_law(w.slug, w.key)
    if rebuild:
        law["records"] = {k: v for k, v in law["records"].items() if v.get("review")}
    law["name"] = w.name or law.get("name", "")
    if not law.get("fek"):
        law["fek"] = resolve_act_fek(w, client)
    fek = law["fek"]
    tl = []
    if fek:
        try:
            tl = client.timeline(fek["et_id"], 0)
        except Exception as exc:  # noqa: BLE001
            log.warning("timeline failed: %s", exc)
    law["et_timeline"] = tl

    cands: dict[int, FekMeta] = {}
    groups = [1, 2] if w.is_decision else [1]  # decisions are amended mostly by other decisions (Β’)
    found_all: list[FekMeta] = []
    for y in range(w.year, until.year + 1):
        found_all += client.search(years=[y], groups=groups, text=w.search_text)
    prefiltered = len(found_all) > 300
    for m in found_all:
        if not prefiltered or _snippet_may_cite(m, w):
            cands[m.et_id] = m
    labels_ok = ("Α ", "Β ") if w.is_decision else ("Α ",)
    for e in tl:
        if e["direction"] < 0 and e["other_label"].startswith(labels_ok) and e["other_id"] not in cands:
            m = _meta_from_label(e["other_id"], e["other_label"], client)
            if m:
                cands[m.et_id] = m
    if fek:
        cands.pop(fek["et_id"], None)
    ordered = sorted(cands.values(), key=lambda m: (m.issue_date or "", m.number))
    found = 0
    for i, meta in enumerate(ordered, 1):
        if progress:
            progress(i, len(ordered), meta)
        try:
            an = analyze_meta(meta, client, store, cfg)
        except Exception as exc:  # noqa: BLE001
            log.exception("failed %s", meta.label)
            continue
        for hit in hits_for(an, {w.key: w.as_match()}):
            if hit.kind in ("reference", "quoted") and hit.confidence < cfg.settings.min_reference_confidence:
                continue
            add_record(law, make_record(meta, an, hit, w))
            found += hit.kind == "amendment"
        if i % 10 == 0:
            store.save_law(w.slug, law)  # checkpoint: a crash mid-way keeps what was found
    law["history"] = {"scanned_at": now_iso(), "through": until.isoformat(), "candidates": len(ordered),
                      "candidate_labels": [m.label for m in ordered],
                      "search_hits": len(found_all), "prefiltered": prefiltered}
    store.save_law(w.slug, law)
    return {"candidates": len(ordered), "amendment_provisions": found, "et_timeline": len(tl)}
