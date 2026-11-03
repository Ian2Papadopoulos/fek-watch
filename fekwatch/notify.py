"""Notifications: a GitHub issue per run with findings, and/or an e-mail digest.

GitHub issue  — enabled automatically inside GitHub Actions (GITHUB_TOKEN + GITHUB_REPOSITORY).
               Everyone watching the repository gets an e-mail from GitHub; closing the issue
               is a convenient way to mark the findings as reviewed.
E-mail       — set SMTP_HOST, SMTP_PORT (465 or 587), SMTP_USER, SMTP_PASSWORD, MAIL_FROM, MAIL_TO.
"""
from __future__ import annotations

import logging
import os
import smtplib
import ssl
from collections import defaultdict
from email.message import EmailMessage

import requests

from .citations import ACTION_LABELS_EL, Locator
from .config import Config
from .report import KIND_LABEL, esc, _fmt_date

log = logging.getLogger(__name__)
IMPORTANT = ("amendment", "derogation")


def _loc(r):
    return Locator(**r["locator"]).describe() if r.get("locator") else ""


def _acts(r):
    return ", ".join(ACTION_LABELS_EL.get(a, a) for a in r.get("actions", []))


def _grouped(records):
    by_law = defaultdict(lambda: defaultdict(list))
    for r in records:
        by_law[r["watched"]][r["source"]["label"]].append(r)
    return by_law


def title_for(records) -> str:
    n_am = sum(1 for r in records if r["kind"] == "amendment")
    laws = sorted({r["watched"] for r in records if r["kind"] in IMPORTANT})
    feks = sorted({r["source"]["label"] for r in records if r["kind"] in IMPORTANT})
    if n_am:
        return f"ΦΕΚ {', '.join(feks[:3])}{'…' if len(feks) > 3 else ''}: τροποποιήσεις σε {', '.join(laws)}"
    return f"ΦΕΚ: {len(records)} νέες αναφορές σε παρακολουθούμενους νόμους"


def markdown_digest(cfg: Config, records) -> str:
    names = {w.key: w.name for w in cfg.laws}
    lines = []
    for law, feks in _grouped(records).items():
        lines.append(f"## {law} — {names.get(law, '')}")
        for label, rs in sorted(feks.items()):
            s = rs[0]["source"]
            lines.append(f"\n**ΦΕΚ {label}** ({_fmt_date(s.get('issue_date'))}) {s.get('act') or ''} · "
                         f"[PDF]({s['pdf'].split('#')[0]}) · [search.et.gr]({s['et_page']})")
            if s.get("act_title"):
                lines.append(f"<sub>{s['act_title'][:250]}</sub>")
            for r in rs:
                where = f"Άρθρο {r['article']}" + (f" παρ. {r['par']}" if r.get("par") else "") if r.get("article") else "Κείμενο"
                page = r["source"].get("page")
                if r["kind"] == "amendment":
                    flag = "" if r.get("target_how") in ("explicit", "alias") else " _(συναγόμενο)_"
                    lines.append(f"- {where} → **{_loc(r) or '—'}** — {_acts(r)}{flag} ([σελ. {page}]({r['source']['pdf']}))")
                else:
                    snippet = (r.get("snippet") or "").replace("\n", " ")[:240]
                    lines.append(f"- {KIND_LABEL.get(r['kind'], r['kind'])}: {where} ([σελ. {page}]({r['source']['pdf']})) — _{snippet}_")
        lines.append("")
    lines.append("---\nΑυτόματος εντοπισμός από το fek-watch — ελέγξτε πάντα το ΦΕΚ. Κλείστε το issue όταν ελεγχθεί.")
    return "\n".join(lines)


def html_digest(cfg: Config, records) -> str:
    names = {w.key: w.name for w in cfg.laws}
    parts = ["<div style=\"font-family:system-ui,Arial,sans-serif;font-size:14px\">"]
    for law, feks in _grouped(records).items():
        parts.append(f"<h2 style=\"font-size:17px\">{esc(law)} — {esc(names.get(law, ''))}</h2>")
        for label, rs in sorted(feks.items()):
            s = rs[0]["source"]
            parts.append(f"<p><b><a href=\"{esc(s['pdf'].split('#')[0])}\">ΦΕΚ {esc(label)}</a></b> "
                         f"({esc(_fmt_date(s.get('issue_date')))}) {esc(s.get('act') or '')}<br>"
                         f"<small>{esc((s.get('act_title') or '')[:250])}</small></p><ul>")
            for r in rs:
                where = (f"Άρθρο {r['article']}" + (f" παρ. {r['par']}" if r.get("par") else "")) if r.get("article") else "Κείμενο"
                link = f"<a href=\"{esc(r['source']['pdf'])}\">σελ. {esc(r['source'].get('page'))}</a>"
                if r["kind"] == "amendment":
                    parts.append(f"<li>{esc(where)} → <b>{esc(_loc(r))}</b> — {esc(_acts(r))} ({link})</li>")
                else:
                    parts.append(f"<li>{esc(KIND_LABEL.get(r['kind'], r['kind']))}: {esc(where)} ({link}) — "
                                 f"<i>{esc((r.get('snippet') or '')[:240])}</i></li>")
            parts.append("</ul>")
    parts.append("<p style=\"color:#777\">Αυτόματος εντοπισμός από το fek-watch — ελέγξτε πάντα το ΦΕΚ.</p></div>")
    return "".join(parts)


def github_issue(cfg: Config, records) -> str | None:
    token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    api = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    body = markdown_digest(cfg, records)
    if len(body) > 60000:
        body = body[:60000] + "\n\n… (περικόπηκε — δείτε την αναφορά)"
    payload = {"title": title_for(records), "body": body, "labels": ["fek-watch"]}
    r = requests.post(f"{api}/repos/{repo}/issues", json=payload, headers=headers, timeout=30)
    if r.status_code == 422:  # label may not exist yet; retry without
        payload.pop("labels")
        r = requests.post(f"{api}/repos/{repo}/issues", json=payload, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json().get("html_url")


def email(cfg: Config, records) -> bool:
    host = os.environ.get("SMTP_HOST")
    to = os.environ.get("MAIL_TO")
    if not host or not to:
        return False
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, pwd = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    msg = EmailMessage()
    msg["Subject"] = title_for(records)
    msg["From"] = os.environ.get("MAIL_FROM", user or "fek-watch")
    msg["To"] = to
    msg.set_content(markdown_digest(cfg, records))
    msg.add_alternative(html_digest(cfg, records), subtype="html")
    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, context=ctx, timeout=60) as s:
            if user:
                s.login(user, pwd or "")
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=60) as s:
            s.starttls(context=ctx)
            if user:
                s.login(user, pwd or "")
            s.send_message(msg)
    return True


def send_all(cfg: Config, records) -> None:
    important = [r for r in records if r["kind"] in IMPORTANT or r["kind"] in ("reference", "mention", "quoted")]
    if not important:
        return
    try:
        url = github_issue(cfg, important)
        if url:
            print(f"GitHub issue: {url}")
    except Exception as exc:  # noqa: BLE001
        log.error("GitHub issue failed: %s", exc)
    try:
        if email(cfg, important):
            print("E-mail sent.")
    except Exception as exc:  # noqa: BLE001
        log.error("E-mail failed: %s", exc)
