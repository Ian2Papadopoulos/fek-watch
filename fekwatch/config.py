"""Watchlist / settings loading (watchlist.toml)."""
from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .citations import act_key, parse_act_key


@dataclass
class WatchedAct:
    key: str           # canonical key, e.g. "ν. 4495/2017"
    kind: str
    number: str
    year: int
    name: str = ""
    aliases: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return slugify(self.key)

    def as_match(self) -> dict:
        return {"kind": self.kind, "number": self.number, "year": self.year, "aliases": self.aliases}


@dataclass
class Settings:
    lookback_days: int = 14
    analyze_groups: list[str] = field(default_factory=lambda: ["Α"])
    mention_groups: list[str] = field(default_factory=lambda: ["Β", "Δ", "Α.Α.Π."])
    min_reference_confidence: float = 0.8
    keep_pdfs: bool = False
    site_title: str = "FEK Watch"


@dataclass
class Config:
    root: Path
    settings: Settings
    laws: list[WatchedAct]

    def get(self, key_or_text: str) -> WatchedAct | None:
        kind, num, year = parse_act_key(key_or_text)
        k = act_key(kind, num, year)
        return next((w for w in self.laws if w.key == k), None)

    def watched_map(self) -> dict[str, dict]:
        return {w.key: w.as_match() for w in self.laws}

    def aliases(self) -> dict[str, list[str]]:
        return {w.key: w.aliases for w in self.laws if w.aliases}


_SLUG_KIND = {"ν.": "n", "π.δ.": "pd", "ν.δ.": "nd", "α.ν.": "an", "β.δ.": "bd", "π.ν.π.": "pnp"}


def slugify(key: str) -> str:
    kind, num, year = parse_act_key(key)
    return f"{_SLUG_KIND.get(kind, 'act')}-{re.sub(r'[^0-9a-z.]', '', num)}-{year}"


def watched_from(act: str, name: str = "", aliases: list[str] | None = None) -> WatchedAct:
    kind, num, year = parse_act_key(act)
    return WatchedAct(act_key(kind, num, year), kind, num, year, name, list(aliases or []))


def load(root: Path | str = ".") -> Config:
    root = Path(root).resolve()
    path = root / "watchlist.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    s = data.get("settings", {})
    settings = Settings(**{k: v for k, v in s.items() if k in Settings.__dataclass_fields__})
    laws = [watched_from(l["act"], l.get("name", ""), l.get("aliases", [])) for l in data.get("law", [])]
    return Config(root, settings, laws)


def add_law_to_file(root: Path, act: str, name: str = "", aliases: list[str] | None = None) -> WatchedAct:
    w = watched_from(act, name, aliases)
    path = Path(root) / "watchlist.toml"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if re.search(r'act\s*=\s*"' + re.escape(w.key) + '"', text):
        return w
    al = ", ".join('"' + a.replace('"', '\\"') + '"' for a in w.aliases)
    block = f'\n[[law]]\nact = "{w.key}"\nname = "{name.replace(chr(34), chr(92) + chr(34))}"\naliases = [{al}]\n'
    path.write_text(text.rstrip() + "\n" + block, encoding="utf-8")
    return w
