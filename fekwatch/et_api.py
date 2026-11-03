"""Client for the National Printing Office (Εθνικό Τυπογραφείο) search API.

This is the same public JSON API that powers https://search.et.gr. Endpoints used:

* POST /searchbydate        {"datePublished": "YYYY-MM-DD"}       issues released that day
* POST /simplesearch        year/issue/number/full-text/date-range search
* POST /searchlegislation   law number -> FEK
* GET  /documententitybyid/{id}
* GET  /timeline/{id}/{0|1} ET's curated links (0: incoming, 1: outgoing; max 100 rows)

PDFs live in public blob storage at a predictable URL (see `pdf_url`).
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass, asdict
from datetime import date, datetime
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger(__name__)

API = "https://searchetv99.azurewebsites.net/api"
BLOB = "https://ia37rg02wpsa01.blob.core.windows.net/fek"
FEK_PAGE = "https://search.et.gr/el/fek/?fekId={id}"
USER_AGENT = "fek-watch/0.1 (gazette amendment monitor)"

ISSUE_GROUPS = {
    1: "Α", 2: "Β", 3: "Γ", 4: "Δ", 5: "Ν.Π.Δ.Δ.", 6: "Α.Π.Σ.", 7: "ΠΑΡΑΡΤΗΜΑ", 8: "Δ.Ε.Β.Ι.",
    9: "Α.ΕΙ.Δ.", 10: "Α.Σ.Ε.Π.", 11: "ΠΡΑ.Δ.Ι.Τ.", 12: "Δ.Δ.Σ.", 13: "Ο.Π.Κ.", 14: "Υ.Ο.Δ.Δ.", 15: "Α.Α.Π.",
}
GROUP_BY_NAME = {v: k for k, v in ISSUE_GROUPS.items()}
GROUP_BY_NAME.update({"A": 1, "B": 2, "Γ": 3, "D": 4, "ΑΑΠ": 15, "ΥΟΔΔ": 14})
# searchlegislation catalogue ids
LEGISLATION_CATALOGUE = {"ν.": "1", "π.δ.": "2", "π.ν.π.": "3", "β.δ.": "102", "ν.δ.": "301", "α.ν.": "101"}


def group_code(name_or_code: str | int) -> int:
    if isinstance(name_or_code, int):
        return name_or_code
    s = str(name_or_code).strip().rstrip("’'΄")
    if s.isdigit():
        return int(s)
    if s in GROUP_BY_NAME:
        return GROUP_BY_NAME[s]
    raise ValueError(f"Unknown FEK issue group: {name_or_code!r}")


def pdf_url(group: int, year: int, number: int) -> str:
    return f"{BLOB}/{group:02d}/{year}/{year}{group:02d}{number:05d}.pdf"


def _parse_date(s: str | None) -> str | None:
    if not s:
        return None
    for fmt in ("%m/%d/%Y %H:%M:%S", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


@dataclass
class FekMeta:
    et_id: int
    group: int
    number: int
    year: int
    issue_date: str | None
    release_date: str | None
    pages: int
    snippet: str | None = None

    @property
    def group_name(self) -> str:
        return ISSUE_GROUPS.get(self.group, str(self.group))

    @property
    def label(self) -> str:
        return f"{self.group_name} {self.number}/{self.year}"

    @property
    def pdf(self) -> str:
        return pdf_url(self.group, self.year, self.number)

    @property
    def et_page(self) -> str:
        return FEK_PAGE.format(id=self.et_id)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(label=self.label, pdf=self.pdf, et_page=self.et_page)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FekMeta":
        return cls(**{k: d.get(k) for k in ("et_id", "group", "number", "year", "issue_date",
                                             "release_date", "pages", "snippet")})


def _row_to_meta(r: dict, prefix: str = "search") -> FekMeta | None:
    g = lambda k: r.get(f"{prefix}_{k}")  # noqa: E731
    if not g("ID"):
        return None
    issue_date = _parse_date(g("IssueDate"))
    year = int(issue_date[:4]) if issue_date else int(str(g("PrimaryLabel")).split("/")[-1])
    snippet = None
    if g("MatchedText"):
        try:
            snippet = base64.b64decode(g("MatchedText")).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            snippet = None
    return FekMeta(
        et_id=int(g("ID")), group=int(g("IssueGroupID")), number=int(g("DocumentNumber")), year=year,
        issue_date=issue_date, release_date=_parse_date(g("PublicationDate")), pages=int(g("Pages") or 0),
        snippet=snippet,
    )


class EtClient:
    def __init__(self, delay: float = 0.3, timeout: float = 60.0):
        self.s = requests.Session()
        retry = Retry(total=5, backoff_factor=1.5, status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset({"GET", "POST"}))
        self.s.mount("https://", HTTPAdapter(max_retries=retry))
        self.s.headers.update({"User-Agent": USER_AGENT, "Origin": "https://search.et.gr",
                               "Referer": "https://search.et.gr/"})
        self.delay = delay
        self.timeout = timeout
        self._last = 0.0

    # ------------------------------------------------------------------ low level
    def _wait(self):
        dt = time.monotonic() - self._last
        if dt < self.delay:
            time.sleep(self.delay - dt)
        self._last = time.monotonic()

    def _call(self, method: str, path: str, body: dict | None = None):
        self._wait()
        url = f"{API}{path}"
        if method == "POST":
            resp = self.s.post(url, json=body, timeout=self.timeout)
        else:
            resp = self.s.get(url, timeout=self.timeout)
        resp.raise_for_status()
        d = resp.json()
        if d.get("status") != "ok":
            raise RuntimeError(f"ET API error for {path}: {d.get('message')}")
        data = d.get("data")
        if isinstance(data, str):
            data = json.loads(data) if data else []
        return data or []

    # ------------------------------------------------------------------ searches
    def released_on(self, day: date | str) -> list[FekMeta]:
        rows = self._call("POST", "/searchbydate", {"datePublished": str(day)})
        return [m for m in (_row_to_meta(r) for r in rows) if m]

    def search(self, *, years: list[int] | None = None, groups: list[int] | None = None,
               number: str | int = "", text: str = "",
               published: tuple[str, str] | None = None, released: tuple[str, str] | None = None) -> list[FekMeta]:
        body = {
            "selectYear": [str(y) for y in (years or [])],
            "selectIssue": [str(g) for g in (groups or [])],
            "documentNumber": str(number or ""),
            "searchText": text,
            "datePublished": f"{published[0]} {published[1]}" if published else "",
            "dateReleased": f"{released[0]} {released[1]}" if released else "",
        }
        rows = self._call("POST", "/simplesearch", body)
        out = [m for m in (_row_to_meta(r) for r in rows) if m]
        if groups:  # the API occasionally ignores the issue filter; enforce it client-side
            out = [m for m in out if m.group in set(groups)]
        return out

    def find_act(self, kind: str, number: str | int, year: int) -> list[dict]:
        """Locate the FEK that published an act (law, decree...)."""
        cat = LEGISLATION_CATALOGUE.get(kind)
        if not cat:
            return []
        rows = self._call("POST", "/searchlegislation",
                          {"legislationCatalogues": cat, "legislationNumber": str(number), "selectYear": [str(year)]})
        out = []
        for r in rows:
            m = _row_to_meta(r)
            if m:
                d = m.to_dict()
                d["description"] = r.get("search_Description")
                d["act_number"] = r.get("search_LawProtocolNumber")
                out.append(d)
        return out

    def document(self, et_id: int) -> dict:
        rows = self._call("GET", f"/documententitybyid/{et_id}")
        info = {}
        for r in rows:
            for k, v in r.items():
                info[k.replace("documententitybyid_", "")] = v
        return info

    def timeline(self, et_id: int, direction: int = 0) -> list[dict]:
        rows = self._call("GET", f"/timeline/{et_id}/{direction}")
        out = []
        for r in rows:
            out.append({
                "other_id": int(r["timeline_OtherDocumentID"]),
                "other_label": r["timeline_PrimaryLabel"],
                "relation": r.get("timeline_EnglishLabel"),
                "relation_el": r.get("timeline_GreekLabel"),
                "relation_id": int(r.get("timeline_RelationshipTypeID", -1)),
                "direction": int(r.get("timeline_Direction", 0)),
            })
        return out

    # ------------------------------------------------------------------ PDFs
    def download(self, meta: FekMeta, cache_dir: Path) -> Path:
        path = cache_dir / "pdf" / f"{meta.group:02d}" / str(meta.year) / f"{meta.year}{meta.group:02d}{meta.number:05d}.pdf"
        if path.exists() and path.stat().st_size > 1000:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._wait()
        with self.s.get(meta.pdf, timeout=max(self.timeout, 180), stream=True) as resp:
            resp.raise_for_status()
            tmp = path.with_suffix(".part")
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(1 << 16):
                    fh.write(chunk)
            tmp.replace(path)
        return path
