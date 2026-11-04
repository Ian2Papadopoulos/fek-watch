"""Local web interface:  python -m fekwatch ui   (opens http://127.0.0.1:8765)

Standard library only. Listens on 127.0.0.1, so it is reachable only from this computer.
Long tasks (history, daily, consolidate) run as background jobs whose output streams to the page.
"""
from __future__ import annotations

import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import config as cfgmod
from .citations import ACTION_LABELS_EL, Locator, parse_act_key
from .normalize import fold
from .store import Store
from .glossary import TIP_CSS, as_dict

JOBS: dict[str, dict] = {}
JOB_LOCK = threading.Lock()
KIND_EL = {"amendment": "Τροποποίηση", "derogation": "Παρέκκλιση", "reference": "Αναφορά",
           "quoted": "Αναφορά σε κείμενο που αντικαθίσταται", "mention": "Αναφορά σε απόφαση"}


# --------------------------------------------------------------------------- jobs

def start_job(root: Path, args: list[str], title: str) -> str:
    jid = uuid.uuid4().hex[:8]
    job = {"id": jid, "title": title, "args": args, "status": "running", "log": [], "started": time.time(),
           "ended": None}
    with JOB_LOCK:
        if any(j["status"] == "running" for j in JOBS.values()):
            raise RuntimeError("Τρέχει ήδη μια εργασία· περιμένετε να ολοκληρωθεί.")
        JOBS[jid] = job

    def run():
        try:
            p = subprocess.Popen([sys.executable, "-m", "fekwatch", "--root", str(root), *args],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(root),
                                 text=True, encoding="utf-8", errors="replace", bufsize=1,
                                 env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"})
            buf = ""
            while True:
                ch = p.stdout.read(1)
                if not ch:
                    break
                if ch in "\r\n":
                    if buf.strip():
                        with JOB_LOCK:
                            log = job["log"]
                            if buf.lstrip().startswith("[") and log and log[-1].lstrip().startswith("["):
                                log[-1] = buf  # progress line "[12/134] ..." replaces the previous one
                            else:
                                log.append(buf)
                            del log[:-400]
                    buf = ""
                else:
                    buf += ch
            if buf.strip():
                job["log"].append(buf)
            job["status"] = "done" if p.wait() == 0 else "failed"
        except Exception as exc:  # noqa: BLE001
            job["log"].append(f"Σφάλμα: {exc}")
            job["status"] = "failed"
        job["ended"] = time.time()

    threading.Thread(target=run, daemon=True).start()
    return jid


# --------------------------------------------------------------------------- data helpers

def laws_payload(root: Path) -> list[dict]:
    cfg = cfgmod.load(root)
    store = Store(root)
    out = []
    for w in cfg.laws:
        law = store.load_law(w.slug, w.key)
        recs = list(law["records"].values())
        am = [r for r in recs if r["kind"] == "amendment"]
        last = max(am, key=lambda r: r["source"].get("issue_date") or "", default=None)
        out.append({
            "key": w.key, "name": w.name, "aliases": w.aliases, "slug": w.slug,
            "fek": (law.get("fek") or {}).get("label"), "fek_pdf": (law.get("fek") or {}).get("pdf"),
            "n_feks": len({r["source"]["et_id"] for r in am}), "n_amend": len(am), "n_other": len(recs) - len(am),
            "last": last["source"].get("issue_date") if last else None,
            "last_label": last["source"]["label"] if last else None,
            "history": bool(law.get("history")),
            "has_page": (root / "site" / "laws" / f"{w.slug}.html").exists(),
            "has_cons": (root / "site" / "laws" / f"{w.slug}-consolidated.html").exists(),
        })
    return out


def recent_payload(root: Path, n: int = 6) -> list[dict]:
    """The latest FEKs that amended a watched act: one row per (FEK, act)."""
    cfg = cfgmod.load(root)
    store = Store(root)
    groups: dict[tuple, dict] = {}
    for w in cfg.laws:
        for r in store.load_law(w.slug, w.key)["records"].values():
            if r["kind"] != "amendment":
                continue
            s = r["source"]
            g = groups.setdefault((s["label"], w.key), {
                "fek": s["label"], "date": s.get("issue_date"), "act": s.get("act"), "law": w.key, "slug": w.slug,
                "pdf": s["pdf"], "page": s.get("page"), "n": 0, "articles": set()})
            g["n"] += 1
            if r.get("article"):
                g["articles"].add(str(r["article"]))
            if s.get("page") and (not g["page"] or s["page"] < g["page"]):
                g["page"], g["pdf"] = s["page"], s["pdf"]
    rows = sorted(groups.values(), key=lambda g: (g["date"] or "", g["fek"]), reverse=True)[:n]
    for g in rows:
        g["articles"] = len(g["articles"])
    return rows


def _rec_text(r: dict) -> str:
    pl = {p["id"]: p["text"] for p in r.get("payloads", [])}
    t = re.sub(r"⟦(\d+)⟧", lambda m: "«" + pl.get(int(m.group(1)), "") + "»", r.get("instruction", ""))
    return t or r.get("snippet", "")


def search_records(root: Path, q: str, law: str | None, kinds: set[str], article: str | None,
                   year: str | None, limit: int = 200) -> dict:
    cfg = cfgmod.load(root)
    store = Store(root)
    terms = [fold(t) for t in q.split() if t.strip()]
    results = []
    total = 0
    for w in cfg.laws:
        if law and w.key != law:
            continue
        for r in store.load_law(w.slug, w.key)["records"].values():
            if kinds and r["kind"] not in kinds:
                continue
            arts = r.get("articles") or [(r.get("locator") or {}).get("article")]
            if article and fold(article).upper() not in [fold(a or "").upper() for a in arts]:
                continue
            if year and not (r["source"].get("issue_date") or "").startswith(year):
                continue
            text = _rec_text(r)
            hay = fold(" ".join([text, r["source"]["label"], r["source"].get("act") or "", r.get("article_title", ""),
                                 r["source"].get("act_title") or ""]))
            if terms and not all(t in hay for t in terms):
                continue
            total += 1
            loc = Locator(**r["locator"]).describe() if r.get("locator") else ""
            results.append({
                "law": w.key, "slug": w.slug, "kind": r["kind"], "kind_el": KIND_EL.get(r["kind"], r["kind"]),
                "fek": r["source"]["label"], "date": r["source"].get("issue_date"), "act": r["source"].get("act"),
                "where": (f"άρθρο {r['article']}" + (f" παρ. {r['par']}" if r.get("par") else "")) if r.get("article") else "",
                "target": loc, "actions": ", ".join(ACTION_LABELS_EL.get(a, a) for a in r.get("actions", [])),
                "action_keys": r.get("actions", []),
                "title": r.get("article_title", "")[:200], "pdf": r["source"]["pdf"], "et_page": r["source"]["et_page"],
                "text": text[:1500], "inferred": r.get("target_how") not in ("explicit", "alias", ""),
            })
    results.sort(key=lambda x: (x["date"] or "", x["fek"]), reverse=True)
    return {"total": total, "results": results[:limit]}


def lookup_act(root: Path, q: str) -> dict:
    """Is the query a law reference? Look it up on search.et.gr."""
    from .et_api import EtClient
    try:
        kind, num, year = parse_act_key(q)
    except ValueError:
        return {"is_act": False}
    key = f"{kind} {num}/{year}"
    cfg = cfgmod.load(root)
    out = {"is_act": True, "key": key, "watched": cfg.get(key) is not None}
    try:
        if kind == "υ.α.":
            from .pipeline import resolve_act_fek
            f = resolve_act_fek(cfgmod.watched_from(q), EtClient())
            found = [f] if f else []
        else:
            found = EtClient().find_act(kind, num, year)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)
        return out
    if found:
        f = [x for x in found if x.get("group") == 1] or found
        out.update(fek=f[0]["label"], date=f[0].get("issue_date"), title=(f[0].get("description") or ""),
                   pdf=f[0]["pdf"], et_page=f[0]["et_page"])
    return out


# --------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    root: Path = Path(".")

    def log_message(self, *a):  # quiet
        pass

    def _json(self, obj, status=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _local_only(self) -> bool:
        # Refuse cross-site requests: only our own page may call the API.
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        if origin and urlparse(origin).netloc != host:
            self._json({"error": "forbidden"}, 403)
            return False
        return True

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/":
                data = (page().replace("/*TIP_CSS*/", TIP_CSS)
                        .replace("/*GLOSSARY*/null", json.dumps(as_dict(), ensure_ascii=False))).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/laws":
                self._json({"laws": laws_payload(self.root), "recent": recent_payload(self.root),
                            "last_run": Store(self.root).state().get("last_run"), "jobs": list(JOBS.values())[-10:]})
            elif u.path == "/api/search":
                kinds = set(filter(None, qs.get("kinds", "").split(",")))
                self._json(search_records(self.root, qs.get("q", ""), qs.get("law") or None, kinds,
                                          qs.get("article") or None, qs.get("year") or None))
            elif u.path == "/api/lookup":
                self._json(lookup_act(self.root, qs.get("q", "")))
            elif u.path.startswith("/api/jobs/"):
                j = JOBS.get(u.path.rsplit("/", 1)[-1])
                self._json(j or {"error": "not found"}, 200 if j else 404)
            elif u.path.startswith("/site/"):
                self._static(unquote(u.path[len("/site/"):]))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 500)

    def _static(self, rel: str):
        base = (self.root / "site").resolve()
        p = (base / rel).resolve()
        if base not in p.parents and p != base or not p.is_file():
            self._json({"error": "not found"}, 404)
            return
        data = p.read_bytes()
        ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):  # noqa: N802
        if not self._local_only():
            return
        u = urlparse(self.path)
        try:
            b = self._body()
            if u.path == "/api/watch":
                act = b.get("act", "")
                aliases = [a.strip() for a in (b.get("aliases") or "").split(";") if a.strip()]
                w = cfgmod.add_law_to_file(self.root, act, b.get("name", ""), aliases)
                from .sync import push_watchlist
                pushed, msg = push_watchlist(self.root)
                self._json({"ok": True, "key": w.key, "pushed": pushed, "message": msg})
            elif u.path == "/api/unwatch":
                ok = cfgmod.remove_law_from_file(self.root, b.get("act", ""))
                from .sync import push_watchlist
                pushed, msg = push_watchlist(self.root) if ok else (False, "")
                self._json({"ok": ok, "pushed": pushed, "message": msg})
            elif u.path == "/api/run":
                cmd = b.get("cmd")
                act = b.get("act") or "all"
                if cmd == "daily":
                    args, title = ["daily", "--no-notify"], "Έλεγχος νέων ΦΕΚ"
                elif cmd == "history":
                    args, title = ["history", act] + (["--rebuild"] if b.get("rebuild") else []), f"Ιστορικό: {act}"
                elif cmd == "consolidate":
                    args, title = ["consolidate", act], f"Ενοποίηση: {act}"
                elif cmd == "fek":
                    label = b.get("label", "")
                    if not re.match(r"^\s*\S+\s*\d+\s*/\s*\d{4}\s*$", label):
                        raise ValueError("Γράψτε το ΦΕΚ στη μορφή «Α 101/2018».")
                    args, title = ["fek", label], f"Ανάλυση ΦΕΚ {label}"
                elif cmd == "sync":
                    args, title = ["sync"], "Συγχρονισμός με το GitHub"
                elif cmd == "site":
                    args, title = ["site"], "Ανανέωση αναφορών"
                else:
                    raise ValueError("unknown command")
                self._json({"job": start_job(self.root, args, title)})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"error": str(exc)}, 400)


def serve(root: Path, port: int = 8765, open_browser: bool = True):
    Handler.root = Path(root).resolve()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print(f"FEK Watch: {url}   (Ctrl+C για τερματισμό)")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


# --------------------------------------------------------------------------- page

def page() -> str:
    return Path(__file__).with_name("ui_page.html").read_text(encoding="utf-8")
