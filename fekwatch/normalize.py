"""Text normalisation for Greek Government Gazette (ΦΕΚ) text.

FEK PDFs contain a number of typographic traps that break naive matching:

* Latin look-alike letters inside Greek words ("NOMOΣ", "TΗΣ", "Τεύχος A’").
* Mathematical/compatibility symbols instead of Greek letters ("∆" U+2206, "Ω" U+2126).
* Several different apostrophe/keraia characters after issue letters ("Α΄", "Α’", "Α'").
* Non-breaking and other exotic spaces, soft hyphens.
* Words hyphenated at line ends.

Everything here is deterministic and conservative: we only change characters that
are unambiguous in context.
"""
from __future__ import annotations

import re
import unicodedata

# Characters that are always replaced.
_CHAR_MAP = {
    "∆": "Δ",  # INCREMENT -> GREEK CAPITAL DELTA
    "Ω": "Ω",  # OHM SIGN -> GREEK CAPITAL OMEGA
    "µ": "μ",  # MICRO SIGN -> GREEK SMALL MU
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    " ": " ",
    "​": "",
    "­": "",  # soft hyphen
    "\t": " ",
    "‐": "-",
    "‑": "-",
    "‒": "-",
    "–": "-",  # en dash: FEK uses it interchangeably with hyphen
    "−": "-",
    "“": "“",
    "”": "”",
    # Apostrophe / keraia variants used after issue letters and ordinal letters.
    "΄": "’",  # GREEK TONOS
    "ʹ": "’",  # GREEK NUMERAL SIGN
    "ʹ": "’",
    "´": "’",
    "′": "’",
    "'": "’",
    "`": "’",
    "‘": "’",
}
_CHAR_TRANS = str.maketrans(_CHAR_MAP)

# Latin letters that look identical to Greek capitals.
_LATIN_TO_GREEK = str.maketrans({
    "A": "Α", "B": "Β", "E": "Ε", "Z": "Ζ", "H": "Η", "I": "Ι", "K": "Κ",
    "M": "Μ", "N": "Ν", "O": "Ο", "P": "Ρ", "T": "Τ", "X": "Χ", "Y": "Υ",
    "o": "ο",
})
_GREEK_RE = re.compile(r"[Ͱ-Ͽἀ-῿]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_TOKEN_RE = re.compile(r"[A-Za-zͰ-Ͽἀ-῿]+")
# A lone Latin capital used as an issue letter: "Τεύχος A’ 102", "(A΄ 167)".
_LONE_ISSUE_LETTER_RE = re.compile(r"(?<![A-Za-zͰ-Ͽ])([ABEZHIKMNOPTXY])(?=’)")
# Short all-look-alike Latin words followed by a Greek word: "To άρθρο", "H παρ.".
_SHORT_LATIN_WORD_RE = re.compile(
    r"(?<![A-Za-zͰ-Ͽ])([ABEZHIKMNOPTXYo]{1,3})(?=\s+[Ͱ-Ͽἀ-῿])"
)


def _fix_token(m: re.Match) -> str:
    tok = m.group(0)
    if _GREEK_RE.search(tok) and _LATIN_RE.search(tok):
        return tok.translate(_LATIN_TO_GREEK)
    return tok


def normalize_chars(text: str) -> str:
    """Character-level normalisation (safe to apply to any FEK text)."""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_CHAR_TRANS)
    text = _TOKEN_RE.sub(_fix_token, text)
    text = _LONE_ISSUE_LETTER_RE.sub(lambda m: m.group(1).translate(_LATIN_TO_GREEK), text)
    text = _SHORT_LATIN_WORD_RE.sub(lambda m: m.group(1).translate(_LATIN_TO_GREEK), text)
    return text


_FOLD_MAP = str.maketrans({
    "ά": "α", "έ": "ε", "ή": "η", "ί": "ι", "ό": "ο", "ύ": "υ", "ώ": "ω",
    "ϊ": "ι", "ϋ": "υ", "ΐ": "ι", "ΰ": "υ", "ς": "σ",
    "Ά": "α", "Έ": "ε", "Ή": "η", "Ί": "ι", "Ό": "ο", "Ύ": "υ", "Ώ": "ω",
    "Ϊ": "ι", "Ϋ": "υ",
})


def fold(text: str) -> str:
    """Lower-case, accent-free copy of `text` with *identical length* (offsets map 1:1)."""
    out = text.translate(_FOLD_MAP).lower()
    if len(out) != len(text):  # extremely rare (e.g. 'İ'); fall back char by char
        out = "".join((c.translate(_FOLD_MAP).lower() or c)[0] for c in text)
    return out


def collapse_spaces(text: str) -> str:
    return re.sub(r"[ ]{2,}", " ", text).strip()


_LOWER_START_RE = re.compile(r"^[a-zα-ωάέήίόύώϊϋΐΰς]")


def join_lines(lines: list[str]) -> str:
    """Join the visual lines of one paragraph, undoing end-of-line hyphenation."""
    out = ""
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not out:
            out = line
            continue
        if out.endswith("-") and len(out) >= 2 and out[-2].isalpha():
            if _LOWER_START_RE.match(line):
                out = out[:-1] + line  # "Περιβάλ-" + "λοντος"
            else:
                out = out + line  # "Χωροταξίας-" + "Πολεοδομίας"
        else:
            out = out + " " + line
    return collapse_spaces(out)


def strip_accents_lower(text: str) -> str:
    """Accent-insensitive, lower-case form used for fuzzy keyword matching."""
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return text.lower().replace("ς", "σ")


def compact(text: str) -> str:
    """Whitespace-insensitive comparison key (used when locating quoted phrases)."""
    return re.sub(r"\s+", " ", text).strip()
