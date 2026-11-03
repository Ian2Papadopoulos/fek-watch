"""Command line interface:  python -m fekwatch <command> ..."""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from datetime import date
from pathlib import Path

from . import config as cfgmod
from .citations import ACTION_LABELS_EL, Locator
from .et_api import EtClient, FekMeta, group_code
from .store import Store

log = logging.getLogger("fekwatch")


def _ctx(args):
    root = Path(args.root).resolve()
    cfg = cfgmod.load(root)
    return cfg, Store(root), EtClient()


def _watched(cfg, act: str):
    w = cfg.get(act)
    return w or cfgmod.watched_from(act)


def cmd_daily(args):
    from .pipeline import run_daily
    from . import notify, report
    cfg, store, client = _ctx(args)
    since = date.fromisoformat(args.since) if args.since else None
    until = date.fromisoformat(args.until) if args.until else None
    res = run_daily(cfg, store, client, until=until, since=since)
    new = res["new_records"]
    print(f"Window {res['window'][0]} .. {res['window'][1]}: analysed {len(res['checked'])} new issue(s): "
          f"{', '.join(res['checked']) or '-'}")
    if res["errors"]:
        print("Errors:", *res["errors"], sep="\n  ")
    print(f"New findings: {len(new)}")
    for r in new:
        if r["kind"] == "mention":
            print(f"  [mention]   {r['watched']} ← {r['source']['label']}: {r['article_title'][:90]}")
        else:
            print(f"  [{r['kind']}] {r['watched']} ← {r['source']['label']} ({r['source']['act'] or ''}) "
                  f"άρθρο {r['article']}{(' παρ. ' + r['par']) if r['par'] else ''} {r.get('summary', '')}")
    report.build_site(cfg, store)
    if new and not args.no_notify:
        notify.send_all(cfg, new)
    return 0


def cmd_history(args):
    from .pipeline import run_history
    from . import report
    cfg, store, client = _ctx(args)
    acts = [w.key for w in cfg.laws] if args.act == "all" else [args.act]
    for act in acts:
        w = _watched(cfg, act)

        def progress(i, n, meta: FekMeta):
            print(f"\r  [{i}/{n}] {meta.label:<14} {meta.issue_date or ''}", end="", flush=True)

        print(f"Scanning amendment history of {w.key} ...")
        res = run_history(cfg, store, client, w, progress=progress, rebuild=args.rebuild)
        print(f"\n  candidates checked: {res['candidates']}, amending provisions found: {res['amendment_provisions']}, "
              f"ET timeline links: {res['et_timeline']}")
    report.build_site(cfg, store)
    return 0


def _records(store, w, kinds):
    law = store.load_law(w.slug, w.key)
    recs = [r for r in law["records"].values() if not kinds or r["kind"] in kinds]
    recs.sort(key=lambda r: (r["source"].get("issue_date") or "", r["source"]["label"], r["article"], r["par"]))
    return law, recs


def cmd_list(args):
    cfg, store, _ = _ctx(args)
    w = _watched(cfg, args.act)
    kinds = set(args.kind.split(",")) if args.kind else {"amendment"}
    law, recs = _records(store, w, kinds if args.kind != "all" else set())
    if args.format == "json":
        json.dump(recs, sys.stdout, ensure_ascii=False, indent=1)
        return 0
    if args.format == "csv":
        wr = csv.writer(sys.stdout)
        wr.writerow(["issue_date", "fek", "amending_act", "article", "par", "kind", "target", "actions", "page", "pdf"])
        for r in recs:
            wr.writerow([r["source"]["issue_date"], r["source"]["label"], r["source"]["act"], r["article"], r["par"],
                         r["kind"], Locator(**r["locator"]).describe() if r["locator"] else "",
                         "; ".join(ACTION_LABELS_EL.get(a, a) for a in r["actions"]), r["source"]["page"],
                         r["source"]["pdf"]])
        return 0
    fek = law.get("fek") or {}
    print(f"{w.key} {('— ' + (fek.get('description') or '')) if fek else ''}")
    if fek:
        print(f"  δημοσίευση: ΦΕΚ {fek.get('label')} ({fek.get('issue_date')})  {fek.get('pdf')}")
    by_fek: dict[str, list] = {}
    for r in recs:
        by_fek.setdefault(r["source"]["label"], []).append(r)
    print(f"  {len(by_fek)} ΦΕΚ, {len(recs)} διατάξεις ({', '.join(sorted(kinds)) or 'all'})\n")
    for label, rs in by_fek.items():
        s = rs[0]["source"]
        print(f"{s['issue_date']}  ΦΕΚ {label}  {s['act'] or ''}")
        for r in rs:
            loc = Locator(**r["locator"]).describe() if r["locator"] else ""
            acts = ", ".join(ACTION_LABELS_EL.get(a, a) for a in r["actions"])
            print(f"    άρθρο {r['article']}{(' παρ. ' + r['par']) if r['par'] else '':<9} σελ.{s['page'] or '?':<4} "
                  f"[{r['kind']}] {loc} {('— ' + acts) if acts else ''}")
    return 0


def cmd_fek(args):
    """Analyse one gazette issue, e.g.  fekwatch fek "Α 101/2018" """
    from .amendments import hits_for
    from .pipeline import analyze_meta
    cfg, store, client = _ctx(args)
    import re
    m = re.match(r"^\s*(.+?)\s*(\d+)\s*/\s*(\d{4})\s*$", args.label)
    if not m:
        print("Use the form: 'Α 101/2018'")
        return 2
    g, num, year = group_code(m.group(1)), int(m.group(2)), int(m.group(3))
    found = [x for x in client.search(years=[year], groups=[g], number=num) if x.number == num]
    if not found:
        print("Not found on search.et.gr")
        return 1
    meta = found[0]
    an = analyze_meta(meta, client, store, cfg)
    print(f"ΦΕΚ {meta.label} ({meta.issue_date}) — {an.act}: {an.act_title[:150]}")
    print(f"  {len(an.articles)} άρθρα, {len(an.provisions)} διατάξεις, PDF: {meta.pdf}")
    watched = cfg.watched_map()
    if args.act:
        w = _watched(cfg, args.act)
        watched = {w.key: w.as_match()}
    for h in hits_for(an, watched):
        p = h.provision
        loc = Locator(**p.locator).describe() if p.locator else ""
        print(f"\n  [{h.kind}] {h.watched}  άρθρο {p.article} παρ. {p.label or '-'} (σελ. {p.page}) {loc} {p.actions}")
        print("    " + an.render(p, max_payload=200)[:900].replace("\n", "\n    "))
    if args.all:
        for p in an.provisions:
            if p.kind == "amendment":
                print(f"  {p.article}/{p.label}: {p.target} {Locator(**p.locator).describe()} {p.actions}")
    return 0


def cmd_site(args):
    from . import report
    cfg, store, _ = _ctx(args)
    out = report.build_site(cfg, store)
    print(f"Site written to {out}")
    return 0


def cmd_watch(args):
    root = Path(args.root).resolve()
    if args.action == "add":
        w = cfgmod.add_law_to_file(root, args.act, args.name or "", args.alias or [])
        print(f"Watching {w.key}. Run:  python -m fekwatch history \"{w.key}\"  to load its amendment history.")
    else:
        cfg = cfgmod.load(root)
        for w in cfg.laws:
            print(f"{w.key:<16} {w.name}  {('aliases: ' + ', '.join(w.aliases)) if w.aliases else ''}")
    return 0


def cmd_consolidate(args):
    from .consolidate import consolidate_law
    from . import report
    cfg, store, client = _ctx(args)
    acts = [w.key for w in cfg.laws] if args.act == "all" else [args.act]
    for act in acts:
        w = _watched(cfg, act)
        law = store.load_law(w.slug, w.key)
        if args.act == "all" and not any(r["kind"] == "amendment" for r in law["records"].values()):
            continue
        try:
            res = consolidate_law(cfg, store, client, w, as_of=args.as_of)
        except SystemExit as exc:
            print(f"{w.key}: {exc}")
            continue
        path = report.write_consolidated(cfg, store, w, res)
        st = res.stats()
        print(f"Consolidated {w.key}: {st['applied']} changes applied automatically, "
              f"{st['failed']} need manual review. -> {path}")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="fekwatch", description="Monitor the Greek Government Gazette (ΦΕΚ) for amendments.")
    ap.add_argument("--root", default=".", help="project directory (with watchlist.toml)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("daily", help="check newly released issues and notify")
    p.add_argument("--since"), p.add_argument("--until")
    p.add_argument("--no-notify", action="store_true")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("history", help="find all FEKs that amend an act (backfill)")
    p.add_argument("act", help='e.g. "ν. 4495/2017" or "all"')
    p.add_argument("--rebuild", action="store_true", help="re-analyse from scratch (after parser updates)")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("list", help="list stored amendments of an act")
    p.add_argument("act")
    p.add_argument("--kind", help="amendment,reference,quoted,derogation,mention or 'all'")
    p.add_argument("--format", choices=["text", "csv", "json"], default="text")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("fek", help='analyse one issue, e.g. "Α 101/2018"')
    p.add_argument("label")
    p.add_argument("--act", help="only report hits for this act")
    p.add_argument("--all", action="store_true", help="also list every amending provision found")
    p.set_defaults(func=cmd_fek)

    p = sub.add_parser("consolidate", help="build the consolidated text of an act (experimental)")
    p.add_argument("act", help='e.g. "ν. 4495/2017" or "all"')
    p.add_argument("--as-of", help="YYYY-MM-DD: apply amendments published up to this date")
    p.set_defaults(func=cmd_consolidate)

    p = sub.add_parser("site", help="regenerate the HTML report")
    p.set_defaults(func=cmd_site)

    p = sub.add_parser("watch", help="list or add watched acts")
    p.add_argument("action", choices=["list", "add"])
    p.add_argument("act", nargs="?")
    p.add_argument("--name")
    p.add_argument("--alias", action="append")
    p.set_defaults(func=cmd_watch)

    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")
    return args.func(args)
