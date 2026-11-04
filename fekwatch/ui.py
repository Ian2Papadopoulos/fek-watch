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
            if len(results) >= limit:
                continue
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
    results.sort(key=lambda x: x["date"] or "", reverse=True)
    return {"total": total, "results": results}


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
                data = (PAGE.replace("/*TIP_CSS*/", TIP_CSS)
                        .replace("/*GLOSSARY*/null", json.dumps(as_dict(), ensure_ascii=False))).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif u.path == "/api/laws":
                self._json({"laws": laws_payload(self.root), "jobs": list(JOBS.values())[-10:]})
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
                self._json({"ok": True, "key": w.key})
            elif u.path == "/api/unwatch":
                self._json({"ok": cfgmod.remove_law_from_file(self.root, b.get("act", ""))})
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

PAGE = r"""<!doctype html><html lang="el"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>FEK Watch</title>
<style>
:root{--bg:#fbfbfa;--fg:#1d1d1f;--muted:#6b6b70;--line:#e3e3e0;--card:#fff;--accent:#1f5fbf;--accent-bg:#eaf1fc;
--amend:#b4441c;--amend-bg:#fdf0ea;--warn:#a15c00;--warn-bg:#fff5e0;--ok:#2f7d32;--q:#f6f6f3;--mark:#fff2a8}
@media (prefers-color-scheme:dark){:root{--bg:#131315;--fg:#e8e8ea;--muted:#9a9aa2;--line:#2c2c31;--card:#1b1b1f;
--accent:#79a8f0;--accent-bg:#1c2738;--amend:#f0a07c;--amend-bg:#33221b;--warn:#e7b25c;--warn-bg:#33291a;--ok:#7cc47f;--q:#202024;--mark:#5a4d00}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,Arial,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line)}
.wrap{max-width:1100px;margin:0 auto;padding:0 16px}
.top{display:flex;gap:16px;align-items:center;padding:12px 0}
.brand{font-weight:700;font-size:18px;white-space:nowrap}
.search{flex:1;display:flex;gap:8px}
input,select,button,textarea{font:inherit;color:inherit}
input[type=text],input[type=search],select{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:0}
.search input{flex:1;font-size:16px;padding:10px 12px}
button{background:var(--accent);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;white-space:nowrap}
button.sec{background:var(--card);color:var(--fg);border:1px solid var(--line)}
button:disabled{opacity:.5;cursor:default}
nav{display:flex;gap:4px}nav a{padding:8px 12px;border-radius:8px 8px 0 0;color:var(--muted);text-decoration:none;cursor:pointer}
nav a.on{color:var(--fg);background:var(--card);border:1px solid var(--line);border-bottom-color:var(--card);margin-bottom:-1px}
main{padding:20px 0 80px}a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:12px 0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
.muted{color:var(--muted)}.small{font-size:13px}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.badge{display:inline-block;font-size:12px;padding:1px 8px;border-radius:999px;background:var(--accent-bg);color:var(--accent)}
.badge.amend{background:var(--amend-bg);color:var(--amend)}.badge.warn{background:var(--warn-bg);color:var(--warn)}
.stats{display:flex;gap:18px;margin:8px 0}.stats b{display:block;font-size:20px}
.res{border-top:1px solid var(--line);padding:10px 0}.res:first-child{border-top:0}
.res .text{white-space:pre-wrap;font-size:14px;background:var(--q);border-radius:6px;padding:8px 10px;margin-top:6px;display:none}
.res.open .text{display:block}.res .head{cursor:pointer}
mark{background:var(--mark);color:inherit;border-radius:3px}
.filters{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
.log{background:#111;color:#ddd;font:12px/1.45 ui-monospace,Menlo,Consolas,monospace;border-radius:8px;padding:10px;max-height:320px;overflow:auto;white-space:pre-wrap}
.lookup{border-left:4px solid var(--accent)}.hide{display:none}
h2{font-size:18px;margin:6px 0 10px}h3{margin:0;font-size:16px}
label.chk{display:inline-flex;gap:4px;align-items:center;font-size:13px}
/*TIP_CSS*/
.help dt{font-weight:600;margin-top:10px}.help dd{margin:2px 0 0 0;color:var(--muted)}
@media (max-width:640px){.top{flex-wrap:wrap}.brand{width:100%}.search{width:100%}.filters>select,.filters>input{flex:1 1 40%;width:auto!important}.grid{grid-template-columns:1fr}}
</style></head><body>
<header><div class="wrap">
 <div class="top"><div class="brand">FEK Watch</div>
  <form class="search" id="sf"><input type="search" id="q" placeholder="Αναζήτηση: π.χ. «ν. 4759/2020», «ΥΑ ΥΠΕΝ/ΔΑΟΚΑ/66006/2360/2023», «Α 101/2018», «πέργκολα»…" autocomplete="off">
  <button>Αναζήτηση</button></form></div>
 <nav><a data-tab="laws" class="on">Νόμοι</a><a data-tab="search">Αναζήτηση</a><a data-tab="tools">Εργαλεία</a><a data-tab="help">Βοήθεια</a></nav>
</div></header>
<div class="wrap"><main>

<section id="tab-laws">
 <div class="row" style="justify-content:space-between"><h2>Παρακολουθούμενοι νόμοι</h2>
  <div class="row"><button id="b-daily">Έλεγχος νέων ΦΕΚ τώρα</button><a href="/site/index.html" target="_blank">Πλήρης αναφορά ↗</a></div></div>
 <div class="grid" id="laws"></div>
 <div class="card"><h3>Προσθήκη νόμου</h3>
  <form id="addf" class="row" style="margin-top:8px">
   <input type="text" id="a-act" placeholder="π.χ. ν. 4759/2020 ή ΥΑ ΥΠΕΝ/…/2023" required style="width:230px">
   <input type="text" id="a-name" placeholder="Τίτλος (προαιρετικά)" style="flex:1;min-width:200px">
   <input type="text" id="a-alias" placeholder="Ονομασίες χωρίς αριθμό, χωρισμένες με ;" style="flex:1;min-width:200px">
   <button>Προσθήκη και φόρτωση ιστορικού</button></form>
  <div class="small muted" style="margin-top:6px">Η φόρτωση του ιστορικού διαβάζει όλα τα ΦΕΚ Α’ που αναφέρουν τον νόμο· για νόμους με πολλές τροποποιήσεις διαρκεί 15–40 λεπτά.</div>
 </div>
</section>

<section id="tab-search" class="hide">
 <div id="lookup"></div>
 <div class="filters">
  <select id="f-law"><option value="">Όλοι οι νόμοι</option></select>
  <input type="text" id="f-art" placeholder="Άρθρο (π.χ. 29)" style="width:130px">
  <input type="text" id="f-year" placeholder="Έτος" style="width:90px">
  <label class="chk" data-k="amendment"><input type="checkbox" value="amendment" checked> Τροποποιήσεις</label>
  <label class="chk" data-k="derogation"><input type="checkbox" value="derogation"> Παρεκκλίσεις</label>
  <label class="chk" data-k="reference"><input type="checkbox" value="reference"> Αναφορές</label>
  <label class="chk" data-k="mention"><input type="checkbox" value="mention"> Αποφάσεις</label>
  <label class="chk" data-k="quoted"><input type="checkbox" value="quoted"> Σε αντικαθιστάμενο κείμενο</label>
 </div>
 <div class="card"><div id="count" class="small muted"></div><div id="results"></div></div>
</section>

<section id="tab-tools" class="hide">
 <div class="card"><h3>Τι αλλάζει ένα ΦΕΚ;</h3>
  <form id="fekf" class="row" style="margin-top:8px"><input type="text" id="fek" placeholder="π.χ. Α 101/2018" required style="width:170px">
  <button>Ανάλυση</button><span class="small muted">Δείχνει τις διατάξεις του ΦΕΚ που αφορούν τους παρακολουθούμενους νόμους.</span></form></div>
 <div class="card"><h3>Συντήρηση</h3><div class="row" style="margin-top:8px">
  <button class="sec" data-run="consolidate">Ανανέωση ενοποιημένων κειμένων</button>
  <button class="sec" data-run="site">Ανανέωση αναφορών</button>
  <button class="sec" data-run="history-rebuild">Επανανάλυση όλου του ιστορικού</button></div></div>
</section>

<section id="tab-help" class="hide">
 <div class="card"><h2>Πώς δουλεύει, με απλά λόγια</h2>
  <p>Κάθε μέρα το FEK Watch διαβάζει τα νέα ΦΕΚ από το Εθνικό Τυπογραφείο και ψάχνει αν κάποιο αλλάζει τους νόμους
  της λίστας σας. Ό,τι βρίσκει το καταγράφει με σύνδεσμο στη σωστή σελίδα του ΦΕΚ, ώστε να το ελέγξετε με ένα κλικ.</p>
  <ul><li><b>Νόμοι</b>: οι νόμοι που παρακολουθείτε, με σύνδεσμο στη λίστα τροποποιήσεων και στο ενοποιημένο κείμενο.</li>
  <li><b>Αναζήτηση</b>: ψάξτε λέξεις («πέργκολα»), έναν νόμο («ν. 4759/2020») ή ένα ΦΕΚ («Α 101/2018»).</li>
  <li><b>Έλεγχος νέων ΦΕΚ τώρα</b>: κάνει αμέσως τον καθημερινό έλεγχο (γίνεται και αυτόματα στο GitHub).</li>
  <li>Το <b>?</b> δίπλα σε κάθε ετικέτα εξηγεί τι σημαίνει· περάστε το ποντίκι από πάνω.</li></ul>
  <p class="small muted">Το εργαλείο είναι βοήθημα: πριν βασιστείτε σε ένα εύρημα, ανοίξτε το ΦΕΚ από τον σύνδεσμο.</p></div>
 <div class="card help" id="glossary"></div>
</section>

<div class="card hide" id="jobcard"><div class="row" style="justify-content:space-between"><h3 id="jobtitle"></h3><span id="jobstatus" class="badge"></span></div>
 <div class="log" id="joblog" style="margin-top:8px"></div></div>
</main></div>
<script>
const G=/*GLOSSARY*/null;
const tip=k=>G[k]?`<span class="tip" tabindex="0" data-tip="${esc(G[k].text)}" aria-label="${esc(G[k].text)}">?</span>`:"";
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const esc=s=>String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=d=>d?d.slice(8,10)+"."+d.slice(5,7)+"."+d.slice(0,4):"—";
let LAWS=[], timer=null;
async function api(path, body){const r=await fetch(path, body?{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}:{});
 const j=await r.json(); if(!r.ok||j.error) throw new Error(j.error||r.statusText); return j;}
function tab(name){$$("nav a").forEach(a=>a.classList.toggle("on",a.dataset.tab===name));
 ["laws","search","tools","help"].forEach(t=>$("#tab-"+t).classList.toggle("hide",t!==name));}
$$("nav a").forEach(a=>a.onclick=()=>tab(a.dataset.tab));

async function loadLaws(){const d=await api("/api/laws"); LAWS=d.laws;
 $("#laws").innerHTML=LAWS.map(l=>`<div class="card" style="margin:0">
  <div class="row" style="justify-content:space-between"><h3>${esc(l.key)}</h3>${l.history?"":'<span class="badge warn">χωρίς ιστορικό'+tip("history")+'</span>'}</div>
  <div class="small muted">${esc(l.name)}</div>
  <div class="stats"><div><b>${l.n_feks}</b><span class="small muted">τροπ. ΦΕΚ${tip("n_feks")}</span></div><div><b>${l.n_amend}</b><span class="small muted">διατάξεις${tip("n_amend")}</span></div>
   <div><b>${fmt(l.last)}</b><span class="small muted">τελευταία${l.last_label?" ("+esc(l.last_label)+")":""}${tip("last")}</span></div>
   <div><b>${l.n_other}</b><span class="small muted">αναφορές${tip("n_other")}</span></div></div>
  <div class="row small">${l.has_page?`<a href="/site/laws/${l.slug}.html" target="_blank">Τροποποιήσεις ↗</a>`:""}
   ${l.has_cons?`<a href="/site/laws/${l.slug}-consolidated.html" target="_blank">Ενοποιημένο κείμενο ↗</a>`:""}
   <a href="#" data-s="${esc(l.key)}">Αναζήτηση</a>
   <a href="#" data-h="${esc(l.key)}">${l.history?"Ανανέωση ιστορικού":"Φόρτωση ιστορικού"}</a>
   <a href="#" data-c="${esc(l.key)}">Ενοποίηση</a>
   <a href="#" data-r="${esc(l.key)}" style="color:var(--warn)">Αφαίρεση</a></div></div>`).join("")||'<div class="muted">Κανένας νόμος.</div>';
 $("#f-law").innerHTML='<option value="">Όλοι οι νόμοι</option>'+LAWS.map(l=>`<option>${esc(l.key)}</option>`).join("");
 $$("[data-s]").forEach(a=>a.onclick=e=>{e.preventDefault(); $("#f-law").value=a.dataset.s; $("#q").value=""; tab("search"); search();});
 $$("[data-h]").forEach(a=>a.onclick=e=>{e.preventDefault(); run({cmd:"history",act:a.dataset.h});});
 $$("[data-c]").forEach(a=>a.onclick=e=>{e.preventDefault(); run({cmd:"consolidate",act:a.dataset.c});});
 $$("[data-r]").forEach(a=>a.onclick=async e=>{e.preventDefault(); if(!confirm("Αφαίρεση του "+a.dataset.r+" από τη λίστα; Τα δεδομένα του μένουν στον φάκελο data/.")) return;
   await api("/api/unwatch",{act:a.dataset.r}); loadLaws();});
 const running=d.jobs.find(j=>j.status==="running"); if(running && !timer) follow(running.id);}

async function run(body){try{const {job}=await api("/api/run",body); follow(job);}catch(e){alert(e.message);}}
function follow(id){$("#jobcard").classList.remove("hide"); clearInterval(timer);
 const tick=async()=>{const j=await api("/api/jobs/"+id); $("#jobtitle").textContent=j.title;
  const st={running:"σε εξέλιξη…",done:"ολοκληρώθηκε",failed:"απέτυχε"}[j.status]; $("#jobstatus").textContent=st;
  $("#jobstatus").className="badge"+(j.status==="failed"?" warn":""); const L=$("#joblog"); L.textContent=j.log.join("\n"); L.scrollTop=L.scrollHeight;
  if(j.status!=="running"){clearInterval(timer); timer=null; loadLaws();}};
 tick(); timer=setInterval(tick,1000);}

function hl(text, q){let h=esc(text); q.split(/\s+/).filter(t=>t.length>1).forEach(t=>{
  const re=new RegExp("("+t.replace(/[.*+?^${}()|[\]\\]/g,"\\$&")+")","gi"); h=h.replace(re,"<mark>$1</mark>");}); return h;}
async function search(){const q=$("#q").value.trim(); tab("search");
 const kinds=$$(".filters input[type=checkbox]:checked").map(c=>c.value).join(",");
 const p=new URLSearchParams({q, law:$("#f-law").value, article:$("#f-art").value.trim(), year:$("#f-year").value.trim(), kinds});
 $("#count").textContent="Αναζήτηση…";
 const d=await api("/api/search?"+p);
 $("#count").textContent=d.total? `${d.total} αποτελέσματα${d.total>d.results.length?" (εμφανίζονται τα "+d.results.length+")":""}` : "Κανένα αποτέλεσμα.";
 $("#results").innerHTML=d.results.map(r=>`<div class="res"><div class="head">
  <div class="row"><b><a href="${esc(r.pdf)}" target="_blank" onclick="event.stopPropagation()">ΦΕΚ ${esc(r.fek)}</a></b>
   <span class="muted small">${fmt(r.date)} · ${esc(r.act||"")} ${esc(r.where)}</span>
   ${r.kind==="amendment"?(r.action_keys.length?r.action_keys:["modify"]).map(a=>`<span class="badge amend">${esc((G[a]||{}).label||a)}${tip(a)}</span>`).join(" ")
      :`<span class="badge">${esc(r.kind_el)}${tip(r.kind)}</span>`}${r.inferred?'<span class="badge warn">συναγόμενο'+tip("inferred")+'</span>':""}
   <span class="small">${esc(r.law)}${r.target?" → <b>"+esc(r.target)+"</b>":""}</span></div>
  <div class="small muted">${esc(r.title)}</div></div><div class="text">${hl(r.text,q)}</div></div>`).join("");
 $$(".res .head").forEach(h=>h.onclick=()=>h.parentElement.classList.toggle("open"));
 lookup(q);}
async function lookup(q){const box=$("#lookup"); box.innerHTML=""; if(!/\d+\s*\/\s*\d{2,4}/.test(q) && !/^\s*(κ\.?υ\.?α|υ\.?α|ΚΥΑ|ΥΑ)/i.test(q)) return;
 if(/^\s*[ΑΒΓΔA-D]\S*\s+\d+\s*\/\s*\d{4}\s*$/i.test(q) && !/^\s*(ν|π|Ν|Π)/.test(q)){
  box.innerHTML=`<div class="card lookup row">Ψάχνετε το ΦΕΚ <b>${esc(q)}</b>; <button class="sec" id="lk-fek">Ανάλυση του ΦΕΚ</button></div>`;
  $("#lk-fek").onclick=()=>run({cmd:"fek",label:q}); return;}
 try{const d=await api("/api/lookup?q="+encodeURIComponent(q)); if(!d.is_act) return;
  box.innerHTML=`<div class="card lookup"><div class="row" style="justify-content:space-between"><div><b>${esc(d.key)}</b>
   ${d.fek?` · <a href="${esc(d.pdf)}" target="_blank">ΦΕΚ ${esc(d.fek)}</a> (${fmt(d.date)})`:" · δεν βρέθηκε στο search.et.gr"}
   <div class="small muted">${esc(d.title||"")}</div></div>
   ${d.watched?'<span class="badge">παρακολουθείται</span>':(d.fek?'<button id="lk-add">Παρακολούθηση και φόρτωση ιστορικού</button>':"")}</div></div>`;
  if($("#lk-add")) $("#lk-add").onclick=async()=>{await api("/api/watch",{act:d.key,name:d.title||""}); await loadLaws(); run({cmd:"history",act:d.key});};
 }catch(e){}}
$("#sf").onsubmit=e=>{e.preventDefault(); search();};
$$(".filters input, .filters select").forEach(el=>el.onchange=search);
$("#b-daily").onclick=()=>run({cmd:"daily"});
$("#addf").onsubmit=async e=>{e.preventDefault(); try{const r=await api("/api/watch",{act:$("#a-act").value,name:$("#a-name").value,aliases:$("#a-alias").value});
 e.target.reset(); await loadLaws(); run({cmd:"history",act:r.key});}catch(err){alert(err.message);}};
$("#fekf").onsubmit=e=>{e.preventDefault(); run({cmd:"fek",label:$("#fek").value.trim()});};
$$("[data-run]").forEach(b=>b.onclick=()=>{const k=b.dataset.run;
 if(k==="history-rebuild") run({cmd:"history",act:"all",rebuild:true}); else run({cmd:k,act:"all"});});
$$("label.chk[data-k]").forEach(l=>l.insertAdjacentHTML("beforeend", tip(l.dataset.k)));
$$("label.chk .tip").forEach(t=>t.onclick=e=>e.preventDefault());
const groups=[["Τι βρέθηκε",["amendment","derogation","reference","quoted","mention"]],
 ["Τι κάνει η τροποποίηση",["replace","add","repeal","delete","modify","renumber","extend","suspend","interpret","correct"]],
 ["Βεβαιότητα",["inferred"]],
 ["Ενοποιημένο κείμενο",["applied","restated","review","whole_act","nontextual","changed","note","previous","experimental"]],
 ["Είδη πράξεων",["decision"]],
 ["Στήλες και κάρτες",["n_feks","n_amend","last","n_other","history","crosscheck"]]];
$("#glossary").innerHTML="<h2>Τι σημαίνουν οι ετικέτες</h2>"+groups.map(([t,ks])=>`<h3 style="margin-top:14px">${t}</h3><dl>`+
 ks.map(k=>`<dt>${esc(G[k].label)}</dt><dd>${esc(G[k].text)}</dd>`).join("")+"</dl>").join("");
loadLaws();
</script></body></html>"""
