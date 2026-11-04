"""Persistent state as plain JSON files (diff-friendly, versioned in git).

data/state.json            which gazette issues were processed, last run
data/laws/<slug>.json      per watched act: its FEK, all detected amendments/references
data/runs.jsonl            one line per daily run (what was checked, what was found)
cache/                     PDFs and extracted text (re-creatable, not versioned)
"""
from __future__ import annotations

import re
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

from .pdftext import ExtractedDoc

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
EXTRACT_VERSION = 1


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=False) + "\n", encoding="utf-8")
    tmp.replace(path)


class Store:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.data = self.root / "data"
        self.cache = self.root / "cache"

    # ------------------------------------------------------------ state
    def state(self) -> dict:
        return _read_json(self.data / "state.json", {"processed": {}, "last_run": None, "mentions_seen": {}})

    def save_state(self, st: dict) -> None:
        _write_json(self.data / "state.json", st)

    def append_run(self, rec: dict) -> None:
        p = self.data / "runs.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def runs(self, limit: int = 60) -> list[dict]:
        p = self.data / "runs.jsonl"
        if not p.exists():
            return []
        lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]

    # ------------------------------------------------------------ laws
    def law_path(self, slug: str) -> Path:
        return self.data / "laws" / f"{slug}.json"

    def load_law(self, slug: str, key: str) -> dict:
        return _read_json(self.law_path(slug), {"key": key, "fek": None, "records": {}, "et_timeline": [],
                                                "history": None, "updated_at": None})

    def save_law(self, slug: str, d: dict) -> None:
        d["records"] = dict(sorted(d["records"].items(),
                                   key=lambda kv: (kv[1]["source"].get("issue_date") or "", kv[1]["source"]["label"],
                                                   kv[1].get("article", ""), kv[1].get("par", ""), kv[0])))
        old = _read_json(self.law_path(slug), None)
        keys = ("records", "fek", "et_timeline", "history", "name")
        if old is not None and all(old.get(k) == d.get(k) for k in keys):
            d["updated_at"] = old.get("updated_at")  # unchanged: keep the file byte-identical
        else:
            d["updated_at"] = now_iso()
        # keep records ordered chronologically for readable diffs
        d["records"] = dict(sorted(d["records"].items(),
                                   key=lambda kv: (kv[1]["source"].get("issue_date") or "", kv[1]["source"]["label"],
                                                   kv[1].get("article", ""), kv[1].get("par", ""), kv[0])))
        _write_json(self.law_path(slug), d)

    # ------------------------------------------------------------ extracted text cache
    def _extract_path(self, et_id: int) -> Path:
        return self.cache / "text" / f"{et_id}.v{EXTRACT_VERSION}.json.gz"

    def get_extracted(self, et_id: int) -> ExtractedDoc | None:
        p = self._extract_path(et_id)
        if not p.exists():
            return None
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        # Text extracted before fonts without a character map were decoded holds control characters:
        # extract it again (fekwatch.glyphmap).
        if any(_CONTROL_RE.search(p["text"]) for p in d.get("paragraphs", [])):
            return None
        return ExtractedDoc.from_json(d)

    def put_extracted(self, et_id: int, doc: ExtractedDoc) -> None:
        p = self._extract_path(et_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(p, "wt", encoding="utf-8") as fh:
            json.dump(doc.to_json(), fh, ensure_ascii=False)
