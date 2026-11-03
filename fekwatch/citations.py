"""Recognisers for Greek legislative citations, structural locators and amendment verbs.

All patterns run on `fold(text)` (lower-case, accent-free, same length as the original),
so match offsets can be used directly on the original text.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .normalize import fold

# --------------------------------------------------------------------------- acts

ACT_KINDS = {
    "ν": "ν.",       # νόμος
    "πδ": "π.δ.",    # προεδρικό διάταγμα
    "νδ": "ν.δ.",    # νομοθετικό διάταγμα
    "αν": "α.ν.",    # αναγκαστικός νόμος
    "βδ": "β.δ.",    # βασιλικό διάταγμα
    "πνπ": "π.ν.π.",  # πράξη νομοθετικού περιεχομένου
}

# Prefix alternatives (folded text). Order matters: longest first.
_PREFIX = (
    r"(?P<prefix>"
    r"π\.\s?ν\.\s?π\.|πραξ(?:η|ησ)\s+νομοθετικου\s+περιεχομενου"
    r"|π\.\s?δ\.|πδ\b|προεδρικ(?:ο|ου)\s+διαταγματ(?:ο|οσ)"
    r"|ν\.\s?δ\.|νομοθετικ(?:ο|ου)\s+διαταγματ(?:ο|οσ)"
    r"|α\.\s?ν\.|αναγκαστικ(?:οσ|ου|ο)\s+νομ(?:οσ|ου|ο)"
    r"|β\.\s?δ\.|βασιλικ(?:ο|ου)\s+διαταγματ(?:ο|οσ)"
    r"|ν\.|νομ(?:οσ|ου|ο|ων|οι|ουσ)\b|ν(?=\s?\d{3,4}\s?/)"
    r")"
)
_NUMYEAR = r"(?P<num>\d{1,4})\s?/\s?(?:(?P<dd>\d{1,2})\.(?P<mm>\d{1,2})\.)?(?P<year>\d{4}|\d{2})(?![\d/])"
# Optional FEK citation right after: "(Α’ 167)", "(Α΄167)", "(ΦΕΚ 167/Α/2017)", "(Α’ 167/3.11.2017)"
_FEK = (
    r"(?:\s?,?\s?\(\s?(?:φεκ\s)?(?:(?P<iss>[αβγδ])[’]?\s?(?P<fnum>\d{1,5})"
    r"|(?P<fnum2>\d{1,5})\s?/\s?(?P<iss2>[αβγδ])[’]?)[^)]{0,25}\)"
    r"|,\s?(?P<iss3>[αβγδ])’\s?(?P<fnum3>\d{1,5})(?!\d))?"
)
LAW_RE = re.compile(_PREFIX + r"\s?" + _NUMYEAR + _FEK)
# Additional numbers in a list: "των ν. 4067/2012 και 4495/2017", "ν. 4067/2012, 4495/2017"
_LIST_CONT_RE = re.compile(r"\s?(?:,|και|η)\s?" + _NUMYEAR + _FEK)
# Bare number/year (used only for watched-act mention detection, with context checks)
BARE_RE = re.compile(r"(?<![\d/.\-])" + _NUMYEAR)
# Π.Ν.Π. are cited by date: "Π.Ν.Π. της 30.3.2020 (Α’ 75)"
PNP_DATE_RE = re.compile(r"π\.\s?ν\.\s?π\.\s+(?:τησ\s+)?(?P<d>\d{1,2})\.(?P<m>\d{1,2})\.(?P<y>\d{4})")


def _kind_from_prefix(prefix: str) -> str:
    p = prefix.replace(" ", "")
    if p.startswith("π.ν") or p.startswith("πραξ"):
        return "π.ν.π."
    if p.startswith("π.δ") or p.startswith("πδ") or p.startswith("προεδρ"):
        return "π.δ."
    if p.startswith("ν.δ") or p.startswith("νομοθετ"):
        return "ν.δ."
    if p.startswith("α.ν") or p.startswith("αναγκ"):
        return "α.ν."
    if p.startswith("β.δ") or p.startswith("βασιλ"):
        return "β.δ."
    return "ν."


def _full_year(y: str) -> int:
    v = int(y)
    if len(y) == 4:
        return v
    return 2000 + v if v <= 40 else 1900 + v


_ISS = {"α": "Α", "β": "Β", "γ": "Γ", "δ": "Δ"}


@dataclass
class ActRef:
    kind: str  # "ν.", "π.δ.", ...
    number: str
    year: int
    start: int
    end: int
    fek: str | None = None  # e.g. "Α 167"

    @property
    def key(self) -> str:
        return f"{self.kind} {self.number}/{self.year}"


def act_key(kind: str, number: str | int, year: int) -> str:
    return f"{kind} {int(number)}/{int(year)}"


def parse_act_key(s: str) -> tuple[str, str, int]:
    """'ν. 4495/2017' / 'ν.4495/2017' / '4495/2017' / 'π.δ. 18/2018' -> (kind, number, year)."""
    f = fold(s.strip())
    m = re.match(r"^\s*(?:" + _PREFIX + r")?\s?" + _NUMYEAR, f)
    if not m:
        raise ValueError(f"Cannot parse act reference: {s!r}")
    kind = _kind_from_prefix(m.group("prefix")) if m.group("prefix") else "ν."
    return kind, str(int(m.group("num"))), _full_year(m.group("year"))


def find_act_refs(text: str, folded: str | None = None) -> list[ActRef]:
    f = folded if folded is not None else fold(text)
    refs: list[ActRef] = []
    for m in LAW_RE.finditer(f):
        # Avoid matching inside words such as "εν." or decision numbers "ΥΠΕΝ/.../2017".
        if m.start() > 0 and f[m.start() - 1].isalpha() and m.group("prefix").startswith("ν"):
            continue
        kind = _kind_from_prefix(m.group("prefix"))
        refs.append(_mk(kind, m))
        pos = m.end()
        while True:  # lists: "ν. 4067/2012 και 4495/2017"
            c = _LIST_CONT_RE.match(f, pos)
            if not c:
                break
            refs.append(_mk(kind, c))
            pos = c.end()
    for m in PNP_DATE_RE.finditer(f):
        refs.append(ActRef("π.ν.π.", f"{int(m.group('d'))}.{int(m.group('m'))}", int(m.group("y")), m.start(), m.end()))
    refs.sort(key=lambda r: r.start)
    return refs


def _mk(kind: str, m: re.Match) -> ActRef:
    iss = m.group("iss") or m.group("iss2") or m.group("iss3")
    fnum = m.group("fnum") or m.group("fnum2") or m.group("fnum3")
    fek = f"{_ISS[iss]} {int(fnum)}" if iss and fnum else None
    return ActRef(kind, str(int(m.group("num"))), _full_year(m.group("year")), m.start(), m.end(), fek)


def find_mentions(text: str, kind: str, number: str, year: int, aliases: list[str] | None = None,
                  folded: str | None = None) -> list[tuple[int, int, float]]:
    """Find mentions of one specific act. Returns (start, end, confidence).

    Confidence 1.0: proper citation ("ν. 4495/2017"); 0.8: number/year in a list of laws or
    followed by a FEK citation; 0.5: bare "4495/2017" (could be a decision number).
    """
    f = folded if folded is not None else fold(text)
    out: dict[int, tuple[int, int, float]] = {}
    for r in find_act_refs(text, f):
        if r.kind == kind and r.number == str(int(number)) and r.year == int(year):
            out[r.start] = (r.start, r.end, 1.0)
    yy = str(year)
    pat = re.compile(r"(?<![\d/.\-])" + re.escape(str(int(number))) + r"\s?/\s?(?:" + yy + "|" + yy[2:] + r")(?![\d/])")
    for m in pat.finditer(f):
        if any(s <= m.start() < e for s, e, _ in out.values()):
            continue
        after = f[m.end(): m.end() + 14]
        before = f[max(0, m.start() - 40): m.start()]
        if re.match(r"\s?,?\s?\(\s?(?:φεκ\s)?[αβγδ][’]?\s?\d", after):
            conf = 0.8
        elif re.search(r"ν\.\s?\d{1,4}\s?/\s?\d{2,4}[^.;]{0,30}(?:,|και)\s?$", before):
            conf = 0.8
        elif re.search(r"[α-ω]/$|/\s?$", before):
            continue  # part of a protocol number like ΥΠΕΝ/ΔΑΟΚΑ/4495/2017
        else:
            conf = 0.5
        out[m.start()] = (m.start(), m.end(), conf)
    for alias in aliases or []:
        fa = fold(alias)
        for m in re.finditer(re.escape(fa), f):
            if not any(s <= m.start() < e for s, e, _ in out.values()):
                out[m.start()] = (m.start(), m.end(), 0.9)
    return sorted(out.values())


# --------------------------------------------------------------------------- ordinals

_UNITS = ["πρωτ", "δευτερ", "τριτ", "τεταρτ", "πεμπτ", "εκτ", "εβδομ", "ογδο", "ενατ"]
_TENS = ["δεκατ", "εικοστ", "τριακοστ", "τεσσαρακοστ", "πεντηκοστ", "εξηκοστ", "εβδομηκοστ", "ογδοηκοστ", "ενενηκοστ"]
_ORD: dict[str, int] = {}
for _i, _u in enumerate(_UNITS, 1):
    _ORD[_u] = _i
_ORD["δεκατ"] = 10
_ORD["ενδεκατ"] = 11
_ORD["δωδεκατ"] = 12
for _t, _tv in zip(_TENS, range(10, 100, 10)):
    _ORD.setdefault(_t, _tv)
    for _i, _u in enumerate(_UNITS, 1):
        _ORD[f"{_t}ο {_u}"] = _tv + _i
        _ORD[f"{_t}ου {_u}"] = _tv + _i
_ORD["τελευται"] = -1
_ORD["προτελευται"] = -2
_ORD_KEYS = sorted(_ORD, key=len, reverse=True)
ORDINAL_RE = re.compile(r"(?:" + "|".join(re.escape(k) for k in _ORD_KEYS) + r")(?:ο|ου|α|η|ησ|ων|οσ)?\b")


def ordinal_value(word: str) -> int | None:
    w = fold(word).strip()
    for k in _ORD_KEYS:
        if w.startswith(k):
            rest = w[len(k):]
            if rest in ("", "ο", "ου", "α", "η", "ησ", "ων", "οσ"):
                return _ORD[k]
    return None


# Greek numeral letters used for cases: α, β, γ, δ, ε, στ, ζ, η, θ, ι, ια, ιβ ...
_GREEK_NUM = {"α": 1, "β": 2, "γ": 3, "δ": 4, "ε": 5, "στ": 6, "ζ": 7, "η": 8, "θ": 9}


def greek_letter_value(label: str) -> int | None:
    """'α'->1 ... 'θ'->9, 'ι'->10, 'ια'->11, 'κ'->20, 'κα'->21. Doubled 'αα'->1 (sub-case)."""
    s = fold(label).strip("’)(. ")
    if not s:
        return None
    if len(s) >= 2 and len(set(s)) == 1 and s[0] in _GREEK_NUM:  # αα, ββ, ...
        return _GREEK_NUM[s[0]]
    tens = {"ι": 10, "κ": 20, "λ": 30, "μ": 40, "ν": 50}
    if s in _GREEK_NUM:
        return _GREEK_NUM[s]
    if s[0] in tens:
        rest = s[1:]
        if not rest:
            return tens[s[0]]
        if rest in _GREEK_NUM:
            return tens[s[0]] + _GREEK_NUM[rest]
    return None


# --------------------------------------------------------------------------- locators

_ART_NUM = (r"(?P<{n}>\d{{1,4}}(?:[α-ω]{{1,3}}(?![α-ω]))?(?![\d/])|(?:"
            + "|".join(_UNITS + ["δεκατ", "ενδεκατ", "δωδεκατ"] + _TENS) + r")[α-ω]{{0,3}})")
# Labels: "5", "2α", "β’", "αα)", "Α.1"; alphabetic labels are validated afterwards.
_LABEL = r"(?P<{n}>\d{{1,3}}(?:[α-ω](?![α-ω]))?(?![\d/])|[α-ω]{{1,4}}(?:’|\))?(?![α-ω])|[α-ω]\.\d+(?:\.\d+)?)"

LOC_PATTERNS = {
    "title": re.compile(r"(?<![α-ω])(?:ο|του|στον|τον)\s+τιτλ(?:οσ|ου|ο)"),
    "article": re.compile(r"(?<![α-ω])(?:αρθρ(?:ο|ου|α|ων)|αρθρ\.|αρ\.)\s?" + _ART_NUM.format(n="v")),
    "paragraph": re.compile(r"(?<![α-ω])(?:παρ\.|παραγραφ(?:οσ|ου|ο|οι|ων|ουσ))\s?" + _LABEL.format(n="v")),
    "subparagraph": re.compile(r"(?<![α-ω])(?:υποπαρ\.|υποπαραγραφ(?:οσ|ου|ο|οι|ων))\s?" + _LABEL.format(n="v")),
    "case": re.compile(r"(?<![α-ω])(?:περ\.|περιπτωσ(?:η|ησ|εισ|εων))\s?" + _LABEL.format(n="v")),
    "subcase": re.compile(r"(?<![α-ω])(?:υποπερ\.|υποπεριπτωσ(?:η|ησ|εισ|εων))\s?" + _LABEL.format(n="v")),
    "element": re.compile(r"(?<![α-ω])(?:στοιχει(?:ο|ου|α|ων)|στοιχ\.)\s?" + _LABEL.format(n="v")),
    "sentence": re.compile(
        r"(?:(?P<ord>" + ORDINAL_RE.pattern + r")\s+(?:εδαφι(?:ο|ου|α|ων)|εδ\.))"
        r"|(?:(?:εδαφι(?:ο|ου|α)|εδ\.)\s?(?P<v>[α-ω]{1,3}’|\d{1,2}))"
    ),
    "annex": re.compile(r"(?<![α-ω])παραρτημ(?:α|ατοσ|ατα|ατων)\s?(?P<v>[α-ω]{1,3}’?|\d{1,3}|[ivx]{1,5})?"),
}


def norm_label(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.replace(" ", "").strip("’).")
    return v


@dataclass
class Locator:
    article: str | None = None
    paragraph: str | None = None
    subparagraph: str | None = None
    case: str | None = None
    subcase: str | None = None
    element: str | None = None
    sentence: int | None = None
    title: bool = False
    annex: str | None = None
    extra_paragraphs: list[str] = field(default_factory=list)  # "οι παρ. 2 και 3"

    def is_empty(self) -> bool:
        return not any([self.article, self.paragraph, self.subparagraph, self.case, self.subcase,
                        self.element, self.sentence, self.title, self.annex])

    def merged_into(self, parent: "Locator | None") -> "Locator":
        """Fill missing *outer* levels from a parent context (e.g. article from the lead-in)."""
        if parent is None:
            return self
        out = Locator(**asdict(self))
        order = ["annex", "article", "paragraph", "subparagraph", "case", "subcase", "element"]
        # find the outermost level present in self
        present = [lv for lv in order if getattr(self, lv)]
        if not present and self.sentence is None and not self.title:
            return Locator(**asdict(parent))
        outer = present[0] if present else "element"
        for lv in order:
            if lv == outer:
                break
            if getattr(out, lv) is None and getattr(parent, lv) is not None:
                setattr(out, lv, getattr(parent, lv))
        return out

    def describe(self) -> str:
        parts = []
        if self.annex:
            parts.append(f"Παράρτημα {self.annex}")
        if self.article:
            parts.append(f"άρθρο {self.article}")
        if self.title:
            parts.append("τίτλος")
        if self.paragraph:
            ps = [self.paragraph] + self.extra_paragraphs
            parts.append("παρ. " + ", ".join(ps))
        if self.subparagraph:
            parts.append(f"υποπαρ. {self.subparagraph}")
        if self.case:
            parts.append(f"περ. {self.case}")
        if self.subcase:
            parts.append(f"υποπερ. {self.subcase}")
        if self.element:
            parts.append(f"στοιχ. {self.element}")
        if self.sentence:
            s = {-1: "τελευταίο", -2: "προτελευταίο"}.get(self.sentence, f"{self.sentence}o")
            parts.append(f"εδάφιο {s}")
        return ", ".join(parts) if parts else "(ολόκληρη η πράξη)"

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v not in (None, False, [], "")}


def _valid_label(v: str) -> bool:
    v = v.strip("’).")
    if not v:
        return False
    if v[0].isdigit() or "." in v:
        return True
    return greek_letter_value(v) is not None


def parse_locator(text: str, folded: str | None = None) -> Locator:
    """Parse the structural reference in a phrase such as
    'Η περ. β’ της παρ. 2 του άρθρου 5' or 'Το δεύτερο εδάφιο της παρ. 1 του άρθρου 29'.

    Only the first valid occurrence of each level is used, which matches how Greek
    drafting nests references from the innermost unit outwards."""
    f = folded if folded is not None else fold(text)
    loc = Locator()
    for lv, rx in LOC_PATTERNS.items():
        for m in rx.finditer(f):
            if lv == "title":
                loc.title = True
                break
            if lv == "sentence":
                if m.group("ord"):
                    loc.sentence = ordinal_value(m.group("ord"))
                else:
                    v = m.group("v").strip("’")
                    loc.sentence = int(v) if v.isdigit() else greek_letter_value(v)
                if loc.sentence is not None:
                    break
                continue
            if lv == "annex":
                loc.annex = norm_label(m.group("v")) or "-"
                break
            v = m.group("v")
            if lv == "article":
                if v[0].isdigit():
                    loc.article = v.upper()
                else:
                    w = ordinal_value(v)
                    if w is None:
                        continue
                    loc.article = str(w)
                break
            if not _valid_label(v):
                continue
            setattr(loc, lv, norm_label(v))
            if lv == "paragraph":
                pos = m.end()
                while True:
                    more = re.match(r"\s?(?:,|και|εωσ)\s?(?:(?:τησ|των)\s+)?(?:παρ\.|παραγραφ(?:ου|ων|ο|οσ))?\s?"
                                    r"(\d{1,3}[α-ω]?)(?![\d/α-ω])", f[pos:])
                    if not more:
                        break
                    conn = fold(more.group(0)).strip()
                    prev = loc.extra_paragraphs[-1] if loc.extra_paragraphs else loc.paragraph
                    nxt = more.group(1)
                    if conn.startswith("εωσ") and prev and prev.isdigit() and nxt.isdigit():
                        loc.extra_paragraphs += [str(i) for i in range(int(prev) + 1, int(nxt))]
                    loc.extra_paragraphs.append(nxt)
                    pos += more.end()
            break
    return loc


# --------------------------------------------------------------------------- actions

ACTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("replace", re.compile(r"\bαντικαθιστα(?:ται|νται)\b|\bαντικατασταθ(?:ει|ουν)\b")),
    ("add", re.compile(r"\bπροστιθ(?:εται|ενται)\b|\bπροστεθ(?:ει|ουν)\b")),
    ("repeal", re.compile(r"\bκαταργ(?:ειται|ουνται)\b|\bπαυ(?:ει|ουν) να ισχυ(?:ει|ουν)\b")),
    ("delete", re.compile(r"\bδιαγραφ(?:εται|ονται)\b|\bαπαλειφ(?:εται|ονται)\b")),
    ("modify", re.compile(
        r"\bτροποποι(?:ειται|ουνται)\b|\bεπερχ(?:εται|ονται) (?:οι )?(?:εξησ|ακολουθεσ) τροποποιησ"
        r"|\bδιαμορφων(?:εται|ονται)\b|\bσυμπληρων(?:εται|ονται)\b")),
    ("renumber", re.compile(r"\bαναριθμ(?:ειται|ουνται)\b")),
    ("extend", re.compile(r"\bπαρατειν(?:εται|ονται)\b")),
    ("suspend", re.compile(r"\bαναστελλ(?:εται|ονται)\b")),
    ("interpret", re.compile(r"\bερμηνευ(?:εται|ονται)\b|\bαυθεντικ\w* ερμηνει")),
    ("correct", re.compile(r"\bδιορθων(?:εται|ονται)\b")),
]
DEROGATION_RE = re.compile(r"κατα παρεκκλιση|κατ’ εξαιρεση")
HISTORY_CLAUSE_RE = re.compile(
    r"\bοπωσ\s+(?:ισχυ|τροποποιηθηκ|αντικατασταθηκ|προστεθηκ|συμπληρωθηκ|διορθωθηκ|καταργηθηκ|αναριθμηθηκ|διαμορφωθηκ|εχει)"
)

ACTION_LABELS_EL = {
    "replace": "Αντικατάσταση",
    "add": "Προσθήκη",
    "repeal": "Κατάργηση",
    "delete": "Διαγραφή",
    "modify": "Τροποποίηση",
    "renumber": "Αναρίθμηση",
    "extend": "Παράταση",
    "suspend": "Αναστολή",
    "interpret": "Ερμηνεία",
    "correct": "Διόρθωση σφάλματος",
    "derogation": "Παρέκκλιση",
    "reference": "Αναφορά",
    "quoted": "Αναφορά σε παρατιθέμενο κείμενο",
}


def find_actions(folded: str) -> list[tuple[str, int, int]]:
    out = []
    for name, rx in ACTION_PATTERNS:
        for m in rx.finditer(folded):
            out.append((name, m.start(), m.end()))
    out.sort(key=lambda t: t[1])
    return out
