"""Static HTML report (site/): works opened from disk or published with GitHub Pages."""
from __future__ import annotations

import csv
import html
import io
import re
from collections import defaultdict
from pathlib import Path

from .citations import ACTION_LABELS_EL, Locator, find_mentions
from .config import Config, WatchedAct
from .store import Store, now_iso
from .glossary import KIND_KEYS, ACTION_KEYS, TIP_CSS, legend_html, tip

KIND_LABEL = {
    "amendment": "Τροποποίηση",
    "derogation": "Παρέκκλιση",
    "reference": "Αναφορά",
    "quoted": "Αναφορά μέσα σε κείμενο που αντικαθίσταται",
    "mention": "Αναφορά σε απόφαση / άλλο τεύχος",
}

FONT_LINK = ('<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
             '<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&display=swap" rel="stylesheet">')

# Same tokens as the local interface (fekwatch/ui_page.html): grey ground, one white sheet,
# the Greek state blue as the only accent, neutral badges.
CSS = """
:root{--ground:#f6f7f9;--surface:#fff;--raised:#f4f6f8;--active:#e9ecef;--border:#e6e8eb;--rule:#eef0f2;--control:#cdced1;
--text:#131416;--muted:#5b5e61;--accent:#0d5eaf;--positive:#1e7b4f;--warn-ink:#8a3c0b;--warn-bg:#fff7e6;
--changed-bg:#eceef1;--added-bg:#e7f0fb;--mark:#fff1a8;--line:var(--border)}
@media (prefers-color-scheme:dark){:root{--ground:#0b0b0c;--surface:#151516;--raised:#1d1d1f;--active:#262628;--border:#2a2a2d;
--rule:#222224;--control:#454548;--text:#f2f2f3;--muted:#b0b0b5;--accent:#78aef0;--positive:#3fc07a;--warn-ink:#f1c36b;
--warn-bg:#33270f;--changed-bg:#2a2a2e;--added-bg:#16263a;--mark:#5a4d00}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--text);font:400 15px/1.6 "IBM Plex Sans",system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif;
-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.bar{display:flex;align-items:center;gap:20px;max-width:1140px;margin:0 auto;height:60px;padding:0 20px;font-size:14px}
.bar .brand{display:flex;align-items:center;gap:10px;color:var(--text);font-weight:600;font-size:16px}
.bar .brand:hover{text-decoration:none}.bar .crumbs{display:flex;gap:18px;color:var(--muted)}
main{max-width:1140px;margin:0 auto 24px;background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:40px 48px 48px}
h1{font-size:24px;font-weight:600;line-height:1.3;margin:0 0 6px;letter-spacing:-.01em}
h2{font-size:18px;font-weight:600;margin:48px 0 16px}
h3{font-size:16px;font-weight:600;margin:0}.muted{color:var(--muted)}.small{font-size:13px}
table{width:100%;border-collapse:collapse;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
th,td{text-align:left;padding:12px 16px;border-top:1px solid var(--rule);vertical-align:top}
th{font-size:13px;font-weight:400;color:var(--muted);border-top:0;border-bottom:1px solid var(--border);white-space:nowrap}
tr:first-child td{border-top:0}.num{text-align:right;font-variant-numeric:tabular-nums}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px 24px;margin:16px 0}
details.card>summary{cursor:pointer}
.fekhead{display:flex;flex-wrap:wrap;gap:6px 16px;align-items:baseline}
.badge{display:inline-flex;align-items:center;height:22px;font-size:12px;font-weight:500;padding:0 9px;border-radius:999px;
background:var(--active);color:var(--text);white-space:nowrap;vertical-align:middle}
.badge.amend{background:var(--active);color:var(--text)}.badge.warn{background:var(--warn-bg);color:var(--warn-ink)}
.prov{border-top:1px solid var(--rule);padding:12px 0 8px}.prov:first-of-type{border-top:0}
.prov summary{cursor:pointer;list-style:none}.prov summary::-webkit-details-marker{display:none}
.prov summary:before{content:"▸ ";color:var(--muted)}.prov[open] summary:before{content:"▾ "}
.instr{margin:10px 0 6px 18px;white-space:pre-wrap}.payload{background:var(--raised);border-left:3px solid var(--control);
padding:10px 14px;margin:8px 0 8px 18px;white-space:pre-wrap;font-size:14px;border-radius:0 8px 8px 0}
.payload:before{content:"«";color:var(--muted)}.payload:after{content:"»";color:var(--muted)}
mark{background:var(--mark);color:inherit;padding:0 2px;border-radius:3px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));margin:24px 0;border:1px solid var(--border);border-radius:10px}
.stat{padding:16px 24px;border-left:1px solid var(--rule)}.stat:first-child{border-left:0}
.stat b{display:block;font-size:24px;font-weight:500;font-variant-numeric:tabular-nums}
.toc a{margin-right:16px}
.ok{color:var(--positive)}.warn{color:var(--warn-ink)}
@media (max-width:900px){table{display:block;overflow-x:auto}}
@media (max-width:640px){main{border-radius:0;border-left:0;border-right:0;padding:24px 16px 40px}
.bar{padding:0 16px}table{display:block;overflow-x:auto}th:nth-child(n+5),td:nth-child(n+5){display:none}
h1{font-size:22px}.fekhead{gap:4px 10px}.stat{border-left:0;border-top:1px solid var(--rule)}.stat:first-child{border-top:0}}
td,th{overflow-wrap:anywhere}
"""

MARK_SVG = ('<svg width="26" height="26" viewBox="0 0 28 28" fill="none" aria-hidden="true">'
            '<path d="M7 3.5h10l5.5 5.5v15.5H7z" stroke="#0d5eaf" stroke-width="1.8" stroke-linejoin="round"/>'
            '<path d="M17 3.5V9h5.5" stroke="#0d5eaf" stroke-width="1.8" stroke-linejoin="round"/>'
            '<circle cx="13.5" cy="16" r="3.6" stroke="#0d5eaf" stroke-width="1.8"/>'
            '<path d="m16.2 18.7 2.6 2.6" stroke="#0d5eaf" stroke-width="1.8" stroke-linecap="round"/></svg>')


def bar(rel: str = "", links: list[tuple[str, str]] | None = None) -> str:
    """Top bar shared by every report page: the mark (home) and a few plain links."""
    extra = "".join(f'<a href="{esc(h)}">{esc(t)}</a>' for t, h in (links or []))
    return (f'<header class="bar"><a class="brand" href="{rel}index.html">{MARK_SVG}FEK Watch</a>'
            f'<nav class="crumbs">{extra}</nav></header>')


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _page(title: str, body: str, rel: str = "", updated: str | None = None) -> str:
    return (f"<!doctype html><html lang=\"el\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{esc(title)}</title>{FONT_LINK}<style>{CSS}{TIP_CSS}</style></head><body>{bar(rel)}<main>"
            f"{body}"
            f"<p class=\"small muted\" style=\"margin-top:40px\">Δεδομένα: {esc((updated or now_iso())[:16].replace('T', ' '))} UTC · fek-watch. "
            f"Πηγή: Εθνικό Τυπογραφείο (search.et.gr). Ο αυτόματος εντοπισμός μπορεί να έχει σφάλματα — "
            f"ελέγχετε πάντα το ΦΕΚ (κάθε εύρημα έχει σύνδεσμο στη σελίδα του PDF).</p></main></body></html>")


def _fmt_date(d: str | None) -> str:
    if not d:
        return ""
    y, m, dd = d[:10].split("-")
    return f"{dd}.{m}.{y}"


def _loc(r: dict) -> str:
    return Locator(**r["locator"]).describe() if r.get("locator") else ""


def action_badges(r: dict) -> str:
    acts = r.get("actions") or ["modify"]
    return " ".join(f'<span class="badge amend">{esc(ACTION_LABELS_EL.get(a, a))}{tip(a)}</span>' for a in acts)


def kind_badge(kind: str) -> str:
    return f'<span class="badge">{esc(KIND_LABEL.get(kind, kind))}{tip(kind)}</span>'


def _actions(r: dict) -> str:
    return ", ".join(ACTION_LABELS_EL.get(a, a) for a in r.get("actions", []))


def _render_instruction(r: dict) -> str:
    """Instruction text with the quoted payloads rendered as blocks."""
    instr = r.get("instruction", "")
    payloads = {p["id"]: p for p in r.get("payloads", [])}
    out = []
    pos = 0
    for m in re.finditer(r"⟦(\d+)⟧", instr):
        out.append(f"<div class=\"instr\">{esc(instr[pos:m.start()].strip())}</div>" if instr[pos:m.start()].strip() else "")
        p = payloads.get(int(m.group(1)))
        if p:
            out.append(f"<div class=\"payload\">{esc(p['text'])}</div>")
        pos = m.end()
    tail = instr[pos:].strip()
    if tail:
        out.append(f"<div class=\"instr\">{esc(tail)}</div>")
    return "".join(out)


def _highlight(text: str, w: WatchedAct) -> str:
    ms = find_mentions(text, w.kind, w.number, w.year, w.aliases)
    out, pos = [], 0
    for s, e, _ in ms:
        out.append(esc(text[pos:s]))
        out.append(f"<mark>{esc(text[s:e])}</mark>")
        pos = e
    out.append(esc(text[pos:]))
    return "".join(out)


def _group_by_fek(recs: list[dict]) -> list[tuple[dict, list[dict]]]:
    groups: dict[int, list[dict]] = defaultdict(list)
    for r in recs:
        groups[r["source"]["et_id"]].append(r)
    items = [(rs[0]["source"], sorted(rs, key=lambda r: (_int(r["article"]), _int(r["par"])))) for rs in groups.values()]
    items.sort(key=lambda t: (t[0].get("issue_date") or "", t[0]["number"]), reverse=True)
    return items


def _int(s: str) -> tuple:
    m = re.match(r"(\d+)", s or "")
    return (int(m.group(1)) if m else 10_000, s or "")


def _fek_card(src: dict, rs: list[dict], w: WatchedAct, amend: bool) -> str:
    head = (f"<div class=\"fekhead\"><h3><a href=\"{esc(src['pdf'].split('#')[0])}\">ΦΕΚ {esc(src['label'])}</a></h3>"
            f"<span class=\"muted\">{esc(_fmt_date(src.get('issue_date')))}</span>"
            f"{'<b>' + esc(src['act']) + '</b>' if src.get('act') else ''}"
            f"<a class=\"small\" href=\"{esc(src['et_page'])}\">search.et.gr</a></div>")
    if src.get("act_title"):
        head += f"<div class=\"small muted\">{esc(src['act_title'][:300])}</div>"
    items = []
    for r in rs:
        page = r["source"].get("page")
        where = f"Άρθρο {esc(r['article'])}" if r.get("article") else "Κείμενο"
        if r.get("par"):
            where += f" παρ. {esc(r['par'])}"
        pdf_link = f"<a class=\"small\" href=\"{esc(r['source']['pdf'])}\">σελ. {esc(page)} ↗</a>" if page else ""
        if amend:
            target = esc(_loc(r)) or "—"
            badge = action_badges(r)
            conf = "" if r.get("target_how") in ("explicit", "alias") else \
                f"<span class=\"badge warn\">συναγόμενο{tip('inferred')}</span>"
            body = _render_instruction(r)
            summary = f"{where} → <b>{target}</b> {badge} {conf} {pdf_link}"
        else:
            summary = f"{where} {pdf_link} {kind_badge(r['kind'])}"
            if r.get("article_title") and r["kind"] == "mention":
                summary += f"<div class=\"small muted\">{esc(r['article_title'][:200])}</div>"
            body = f"<div class=\"instr\">{_highlight(r.get('snippet') or r.get('instruction', ''), w)}</div>"
        title = f"<div class=\"small muted\">{esc(r['article_title'][:220])}</div>" if amend and r.get("article_title") else ""
        items.append(f"<details class=\"prov\"><summary>{summary}{title}</summary>{body}</details>")
    return f"<div class=\"card\">{head}{''.join(items)}</div>"


def _by_article(recs: list[dict]) -> str:
    arts: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        affected = r.get("articles") or [(r.get("locator") or {}).get("article")]
        for a in affected:
            arts[a or "—"].append(r)
    rows = []
    for a in sorted(arts, key=lambda s: _int(s)):
        rs = sorted(arts[a], key=lambda r: r["source"].get("issue_date") or "")
        links = ", ".join(
            f"<a href=\"{esc(r['source']['pdf'])}\" title=\"{esc(_loc(r))} — {esc(_actions(r))}\">"
            f"{esc(r['source']['label'])}</a>" for r in rs)
        rows.append(f"<tr><td>{esc(a)}</td><td class=\"num\">{len(rs)}</td><td>{links}</td></tr>")
    return ("<table><thead><tr><th>Άρθρο</th><th class=\"num\">Διατάξεις</th><th>Τροποποιήθηκε από (χρονολογικά)</th>"
            "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>")


def _crosscheck(law: dict, amend_recs: list[dict]) -> str:
    tl = [e for e in law.get("et_timeline", []) if e.get("direction", 0) < 0]
    if not tl:
        return "<p class=\"muted\">Δεν υπάρχουν διαθέσιμα δεδομένα από το Εθνικό Τυπογραφείο.</p>"
    ours = {r["source"]["et_id"] for r in amend_recs}
    ours_labels = {r["source"]["label"] for r in amend_recs}
    by_doc: dict[int, dict] = {}
    for e in tl:
        d = by_doc.setdefault(e["other_id"], {"label": e["other_label"], "rels": set()})
        d["rels"].add(e.get("relation_el") or e.get("relation") or "")
    rows = []
    for oid, d in sorted(by_doc.items(), key=lambda kv: kv[1]["label"]):
        if oid in ours:
            st = "<span class=\"ok\">εντοπίστηκε τροποποίηση</span>"
        elif not d["label"].startswith("Α "):
            st = "<span class=\"muted\">άλλο τεύχος (απόφαση) — δεν μπορεί να τροποποιεί νόμο</span>"
        else:
            st = "<span class=\"warn\">δεν εντοπίστηκε τροποποίηση: ελέγξτε το</span>"
        url = f"https://search.et.gr/el/fek/?fekId={oid}"
        rows.append(f"<tr><td><a href=\"{url}\">{esc(d['label'])}</a></td><td>{esc(', '.join(sorted(d['rels'])))}</td><td>{st}</td></tr>")
    tl_labels = {d["label"] for d in by_doc.values()}
    extra = sorted(ours_labels - tl_labels)
    note = ("<p class=\"small muted\">Το Εθνικό Τυπογραφείο επιστρέφει έως 100 συνδέσεις ανά ΦΕΚ, οπότε για νόμους με πολλές "
            "τροποποιήσεις οι παλαιότερες λείπουν από τη λίστα του.</p>") if len(tl) >= 100 else ""
    extra_html = (f"<p>ΦΕΚ που εντοπίσαμε εμείς αλλά <b>δεν</b> αναφέρονται στο χρονολόγιο του Εθνικού Τυπογραφείου "
                  f"({len(extra)}): {esc(', '.join(extra))}</p>") if extra else ""
    return (note + "<table><thead><tr><th>ΦΕΚ (κατά το Εθνικό Τυπογραφείο)</th><th>Σχέση</th><th>Έλεγχος fek-watch</th></tr>"
            "</thead><tbody>" + "".join(rows) + "</tbody></table>" + extra_html)


def _csv(recs: list[dict]) -> str:
    buf = io.StringIO()
    wr = csv.writer(buf)
    wr.writerow(["ημερομηνία ΦΕΚ", "ΦΕΚ", "τροποποιητική πράξη", "άρθρο", "παράγραφος", "είδος", "τροποποιούμενη διάταξη",
                 "ενέργεια", "σελίδα", "PDF"])
    for r in sorted(recs, key=lambda r: r["source"].get("issue_date") or ""):
        wr.writerow([r["source"].get("issue_date"), r["source"]["label"], r["source"].get("act") or "", r.get("article"),
                     r.get("par"), KIND_LABEL.get(r["kind"], r["kind"]), _loc(r), _actions(r), r["source"].get("page"),
                     r["source"]["pdf"]])
    return "﻿" + buf.getvalue()  # BOM so Excel opens Greek correctly


def law_page(cfg: Config, store: Store, w: WatchedAct) -> tuple[str, dict]:
    law = store.load_law(w.slug, w.key)
    recs = list(law["records"].values())
    amend = [r for r in recs if r["kind"] == "amendment"]
    other = {k: [r for r in recs if r["kind"] == k] for k in ("derogation", "reference", "quoted", "mention")}
    fek = law.get("fek") or {}
    hist = law.get("history") or {}
    n_feks = len({r["source"]["et_id"] for r in amend})
    last = max((r["source"].get("issue_date") or "" for r in amend), default="")
    body = [f"<h1>{esc(w.key)}</h1><p>{esc(w.name or fek.get('description') or '')}</p>"]
    if fek:
        body.append(f"<p class=\"small\">Δημοσίευση: <a href=\"{esc(fek.get('pdf'))}\">ΦΕΚ {esc(fek.get('label'))}</a> "
                    f"({esc(_fmt_date(fek.get('issue_date')))}) · <a href=\"{esc(fek.get('et_page'))}\">search.et.gr</a></p>")
    if hist:
        body.append(f"<p class=\"small muted\">Πλήρες ιστορικό: ελέγχθηκαν {hist.get('candidates')} ΦΕΚ Α’ που αναφέρουν τον νόμο "
                    f"(έως {esc(_fmt_date(hist.get('through')))}), σάρωση {esc(hist.get('scanned_at', '')[:10])}. "
                    f"Μετά από αυτό ενημερώνεται καθημερινά.</p>")
    else:
        body.append("<p class=\"small warn\">Το παλαιότερο ιστορικό δεν έχει σαρωθεί ακόμη· εμφανίζονται μόνο ευρήματα από "
                    "την καθημερινή παρακολούθηση. Εκτελέστε <code>python -m fekwatch history</code>.</p>")
    cons = store.root / "site" / "laws" / f"{w.slug}-consolidated.html"
    links = [f"<a href=\"#amend\">Τροποποιήσεις</a>", "<a href=\"#articles\">Ανά άρθρο</a>",
             "<a href=\"#other\">Αναφορές</a>", "<a href=\"#et\">Διασταύρωση ΕΤ</a>",
             f"<a href=\"{w.slug}.csv\">CSV</a>"]
    if cons.exists():
        links.insert(0, f"<a href=\"{w.slug}-consolidated.html\"><b>Ενοποιημένο κείμενο</b></a>")
    body.append(f"<p class=\"toc\">{' '.join(links)}</p>")
    body.append(f"<div class=\"grid\"><div class=\"stat\"><b>{n_feks}</b>τροποποιητικά ΦΕΚ{tip('n_feks')}</div>"
                f"<div class=\"stat\"><b>{len(amend)}</b>τροποποιητικές διατάξεις{tip('n_amend')}</div>"
                f"<div class=\"stat\"><b>{esc(_fmt_date(last)) or '—'}</b>τελευταία τροποποίηση{tip('last')}</div>"
                f"<div class=\"stat\"><b>{sum(len(v) for v in other.values())}</b>αναφορές{tip('n_other')}</div></div>")
    body.append(legend_html(KIND_KEYS + ACTION_KEYS + ["inferred", "crosscheck"]))
    body.append("<h2 id=\"amend\">Τροποποιήσεις (νεότερες πρώτα)</h2>")
    body.append("".join(_fek_card(src, rs, w, True) for src, rs in _group_by_fek(amend)) or "<p class=\"muted\">Καμία.</p>")
    body.append("<h2 id=\"articles\">Ανά άρθρο του νόμου</h2>")
    body.append(_by_article(amend) if amend else "<p class=\"muted\">—</p>")
    body.append("<h2 id=\"other\">Παρεκκλίσεις και αναφορές</h2>")
    for k in ("derogation", "reference", "quoted", "mention"):
        if other[k]:
            body.append(f"<details><summary><b>{esc(KIND_LABEL[k])}</b> ({len(other[k])})</summary>"
                        + "".join(_fek_card(src, rs, w, False) for src, rs in _group_by_fek(other[k])) + "</details>")
    body.append(f"<h2 id=\"et\">Διασταύρωση με το χρονολόγιο του Εθνικού Τυπογραφείου{tip('crosscheck')}</h2>")
    body.append(_crosscheck(law, amend))
    stats = {"key": w.key, "name": w.name, "fek": fek, "n_feks": n_feks, "n_amend": len(amend), "last": last,
             "n_other": sum(len(v) for v in other.values()), "history": bool(hist)}
    return _page(f"{w.key} — τροποποιήσεις", "".join(body), rel="../", updated=law.get("updated_at")), stats


def build_site(cfg: Config, store: Store) -> Path:
    out = store.root / "site"
    (out / "laws").mkdir(parents=True, exist_ok=True)
    rows, recent = [], []
    for w in cfg.laws:
        page, st = law_page(cfg, store, w)
        (out / "laws" / f"{w.slug}.html").write_text(page, encoding="utf-8")
        law = store.load_law(w.slug, w.key)
        (out / "laws" / f"{w.slug}.csv").write_text(_csv(list(law["records"].values())), encoding="utf-8")
        fek = st["fek"] or {}
        rows.append(
            f"<tr><td><a href=\"laws/{w.slug}.html\"><b>{esc(w.key)}</b></a><div class=\"small muted\">{esc(w.name)}</div></td>"
            f"<td>{('<a href=\"' + esc(fek.get('pdf')) + '\">' + esc(fek.get('label')) + '</a>') if fek else '—'}</td>"
            f"<td class=\"num\">{st['n_feks']}</td><td class=\"num\">{st['n_amend']}</td>"
            f"<td class=\"num\">{esc(_fmt_date(st['last']))}</td><td class=\"num\">{st['n_other']}</td>"
            f"<td>{'Πλήρες' if st['history'] else '<span class=\"warn\">όχι ακόμη</span>'}</td></tr>")
        for r in law["records"].values():
            recent.append((r, w))
    recent.sort(key=lambda t: (t[0]["source"].get("release_date") or t[0]["source"].get("issue_date") or ""), reverse=True)
    st = store.state()
    runs = store.runs(15)
    body = [f"<h1>{esc(cfg.settings.site_title)}</h1>",
            f"<p class=\"muted\">Τελευταίος έλεγχος: {esc((st.get('last_run') or '—')[:16].replace('T', ' '))} UTC · "
            f"παράθυρο {esc(' – '.join(_fmt_date(x) for x in st.get('last_window', [])))}</p>",
            "<h2>Παρακολουθούμενοι νόμοι</h2><table><thead><tr><th>Πράξη</th><th>ΦΕΚ</th>"
            f"<th class=\"num\">Τροπ. ΦΕΚ{tip('n_feks')}</th><th class=\"num\">Διατάξεις{tip('n_amend')}</th><th class=\"num\">Τελευταία{tip('last')}</th>"
            f"<th class=\"num\">Αναφορές{tip('n_other')}</th><th>Ιστορικό{tip('history')}</th></tr></thead><tbody>" + "".join(rows) + "</tbody></table>"]
    def rows_for(pairs):
        out = []
        for r, w in pairs:
            src = r["source"]
            what = (f"{action_badges(r)} {esc(_loc(r))}"
                    if r["kind"] == "amendment" else kind_badge(r["kind"])
                    + (f" <span class=\"small muted\">{esc((r.get('article_title') or '')[:110])}</span>" if r["kind"] == "mention" else ""))
            out.append(f"<tr><td class=\"num\">{esc(_fmt_date(src.get('release_date') or src.get('issue_date')))}</td>"
                       f"<td><a href=\"{esc(src['pdf'])}\">{esc(src['label'])}</a><div class=\"small muted\">{esc(src.get('act') or '')}</div></td>"
                       f"<td><a href=\"laws/{w.slug}.html\">{esc(w.key)}</a></td><td>{what}</td></tr>")
        return "".join(out)
    head_row = "<table><thead><tr><th class=\"num\">Κυκλοφορία</th><th>ΦΕΚ</th><th>Νόμος</th><th>Τι</th></tr></thead><tbody>"
    amend_recent = [(r, w) for r, w in recent if r["kind"] in ("amendment", "derogation")][:25]
    other_recent = [(r, w) for r, w in recent if r["kind"] not in ("amendment", "derogation")][:25]
    body.append("<h2>Πρόσφατες τροποποιήσεις</h2>")
    body.append(head_row + rows_for(amend_recent) + "</tbody></table>" if amend_recent else "<p class=\"muted\">Καμία.</p>")
    body.append("<h2>Πρόσφατες αναφορές (αποφάσεις και άλλες πράξεις)</h2>")
    body.append(head_row + rows_for(other_recent) + "</tbody></table>" if other_recent else "<p class=\"muted\">Καμία.</p>")
    items = []
    for r, w in []:
        src = r["source"]
        what = (f"{action_badges(r)} {esc(_loc(r))}"
                if r["kind"] == "amendment" else kind_badge(r["kind"]))
        items.append(f"<tr><td>{esc(_fmt_date(src.get('release_date') or src.get('issue_date')))}</td>"
                     f"<td><a href=\"{esc(src['pdf'])}\">{esc(src['label'])}</a><div class=\"small muted\">{esc(src.get('act') or '')}</div></td>"
                     f"<td><a href=\"laws/{w.slug}.html\">{esc(w.key)}</a></td><td>{what}</td></tr>")

    if runs:
        rr = "".join(f"<tr><td class=\"num\">{esc(r['at'][:16].replace('T', ' '))}</td><td>{esc(', '.join(r.get('checked', [])) or '—')}</td>"
                     f"<td class=\"num\">{len(r.get('new', []))}</td><td>{esc('; '.join(r.get('errors', []))[:200])}</td></tr>"
                     for r in reversed(runs))
        body.append("<h2>Ημερολόγιο ελέγχων</h2><table><thead><tr><th class=\"num\">Πότε (UTC)</th><th>Νέα ΦΕΚ Α’ που αναλύθηκαν</th>"
                    "<th class=\"num\">Νέα ευρήματα</th><th>Σφάλματα</th></tr></thead><tbody>" + rr + "</tbody></table>")
    (out / "index.html").write_text(_page(cfg.settings.site_title, "".join(body), updated=st.get("last_run")),
                                    encoding="utf-8")
    return out


def write_consolidated(cfg: Config, store: Store, w: WatchedAct, result) -> Path:
    from .consolidate import render_html
    out = store.root / "site" / "laws" / f"{w.slug}-consolidated.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    html_text = render_html(w, result, updated=store.load_law(w.slug, w.key).get("updated_at"))
    if not out.exists() or out.read_text(encoding="utf-8") != html_text:
        out.write_text(html_text, encoding="utf-8")
    build_site(cfg, store)
    return out
