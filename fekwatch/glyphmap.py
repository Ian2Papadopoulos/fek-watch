"""Recover the letters of embedded fonts that have no character map.

Some FEKs (often tables made in Word) embed a subset of Calibri as a CID font with Identity-H encoding and
no /ToUnicode table. A PDF reader then only sees glyph numbers, and the text comes out as control
characters ("\\x14\\x16*\\x14#...") instead of Greek.

The glyph shapes are still in the PDF. Carlito and Arimo (SIL Open Font Licence, bundled in fekwatch/fonts/)
have exactly the widths of Calibri and Arial, and similar outlines. For each glyph of the embedded subset we
take its width from the PDF and draw its outline into a small grid, choose the reference font whose widths
agree with the subset, and pick the character with the same width and the most similar drawing. A glyph that matches nothing well becomes "□",
so a bad guess is visible instead of silent.
"""
from __future__ import annotations

import io
import re
from functools import lru_cache
from pathlib import Path

try:
    from fontTools.pens.basePen import BasePen
    from fontTools.ttLib import TTFont
except ImportError:  # optional: without fontTools the text is left as it is
    TTFont = None
    BasePen = object

FONT_DIR = Path(__file__).with_name("fonts")
GRID = 48
# the em box the drawings are compared in (units of the em): x from -0.05 to 1.15, y from 1.0 down to -0.3
X0, X1, Y0, Y1 = -0.05, 1.15, 1.0, -0.3
UNKNOWN = "□"
CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

CHARSET = (
    "ΆΈΉΊΌΎΏΐΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩΪΫάέήίΰαβγδεζηθικλμνξοπρςστυφχψωϊϋόύώ"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    ".,;:!?()[]{}-–—«»\"'’‘“”/\\%&+=*#@§°·…€$²³½¼¾×<>|_~΄¨"
    "•▪■●□≤≥±√∙"
)
GREEK = re.compile(r"[Ͱ-Ͽ]")


class _Poly(BasePen):
    """Collects the outline as closed polygons (curves flattened)."""

    def __init__(self, glyphset):
        super().__init__(glyphset)
        self.polys: list[list[tuple[float, float]]] = []
        self.cur: list[tuple[float, float]] = []

    def _moveTo(self, pt):
        self.cur = [pt]

    def _lineTo(self, pt):
        self.cur.append(pt)

    def _curveToOne(self, p1, p2, p3):
        p0 = self.cur[-1]
        for i in range(1, 9):
            t = i / 8
            a, b, c, d = (1 - t) ** 3, 3 * t * (1 - t) ** 2, 3 * t * t * (1 - t), t ** 3
            self.cur.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))

    def _qCurveToOne(self, p1, p2):
        p0 = self.cur[-1]
        for i in range(1, 7):
            t = i / 6
            a, b, c = (1 - t) ** 2, 2 * t * (1 - t), t * t
            self.cur.append((a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]))

    def _closePath(self):
        if len(self.cur) > 2:
            self.polys.append(self.cur)
        self.cur = []

    _endPath = _closePath


def _raster(polys, upem: int) -> frozenset[int]:
    """Even-odd fill of the polygons on a GRID x GRID raster; returns the set of filled cells."""
    cells = set()
    edges = []
    for poly in polys:
        pts = [((x / upem - X0) / (X1 - X0) * GRID, (Y0 - y / upem) / (Y0 - Y1) * GRID) for x, y in poly]
        edges += [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))]
    for row in range(GRID):
        yc = row + 0.5
        xs = sorted(x1 + (yc - y1) * (x2 - x1) / (y2 - y1)
                    for (x1, y1), (x2, y2) in edges if (y1 <= yc < y2) or (y2 <= yc < y1))
        for a, b in zip(xs[0::2], xs[1::2]):
            for col in range(max(0, int(a + 0.5)), min(GRID, int(b + 0.5))):
                cells.add(row * GRID + col)
    return frozenset(cells)


def _drawing(font, gname: str):
    gs = font.getGlyphSet()
    pen = _Poly(gs)
    gs[gname].draw(pen)
    return _raster(pen.polys, font["head"].unitsPerEm)


# Free fonts with the same widths as the fonts Greek offices use (SIL Open Font Licence, see fonts/OFL-*.txt).
REFERENCES = {"Calibri": "Carlito-Regular.ttf", "Calibri Bold": "Carlito-Bold.ttf",
              "Arial": "Arimo-Regular.ttf", "Arial Bold": "Arimo-Bold.ttf"}


@lru_cache(maxsize=8)
def _reference(name: str):
    """[(char, width in em, drawing)] for one reference font."""
    font = TTFont(str(FONT_DIR / REFERENCES[name]))
    upem = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    out = []
    for ch in CHARSET:
        g = cmap.get(ord(ch))
        if g:
            out.append((ch, font["hmtx"][g][0] / upem, _drawing(font, g)))
    return out


def _grow(a: frozenset) -> frozenset:
    """The drawing thickened by one cell, so small differences of style between the two fonts are tolerated."""
    out = set(a)
    for c in a:
        r, k = divmod(c, GRID)
        for dr in (-1, 0, 1):
            for dk in (-1, 0, 1):
                if 0 <= r + dr < GRID and 0 <= k + dk < GRID:
                    out.add((r + dr) * GRID + k + dk)
    return frozenset(out)


@lru_cache(maxsize=4096)
def _grown(a: frozenset) -> frozenset:
    return _grow(a)


def _score(a: frozenset, b: frozenset) -> float:
    """How much of each drawing lies on (the thickened) other one: 1.0 for the same shape."""
    if not a and not b:
        return 1.0
    return (len(a & _grown(b)) + len(b & _grown(a))) / max(1, len(a) + len(b))


def _embedded_glyphs(doc, xref: int) -> dict[int, tuple[float, frozenset]] | None:
    """{CID: (width in em, drawing)} for a Type0 font embedded as TrueType or as CID-keyed CFF."""
    name, ext, _t, buf = doc.extract_font(xref)
    if not buf:
        return None
    desc = doc.xref_get_key(xref, "DescendantFonts")[1]
    m = re.search(r"(\d+) 0 R", desc)
    dxref = int(m.group(1)) if m else None
    widths = _pdf_widths(doc, dxref) if dxref else {}
    dw = 1000
    if dxref and doc.xref_get_key(dxref, "DW")[0] == "int":
        dw = int(doc.xref_get_key(dxref, "DW")[1])
    out = {}
    if ext == "ttf":
        if dxref and doc.xref_get_key(dxref, "CIDToGIDMap")[1] not in ("/Identity", "null"):
            return None  # a CID -> glyph table: not handled
        font = TTFont(io.BytesIO(buf))
        upem = font["head"].unitsPerEm
        for gid, gname in enumerate(font.getGlyphOrder()):
            if gid:
                out[gid] = (widths.get(gid, font["hmtx"][gname][0] * 1000 / upem) / 1000, _drawing(font, gname))
        return out
    if ext in ("cid", "cff"):
        from fontTools.cffLib import CFFFontSet
        cff = CFFFontSet()
        cff.decompile(io.BytesIO(buf), None)
        top = cff[cff.fontNames[0]]
        scale = 1 / (top.FontMatrix[0] if getattr(top, "FontMatrix", None) else 0.001)
        for gname in top.charset:
            mm = re.fullmatch(r"cid(\d+)", gname)
            if not mm:
                continue
            cid = int(mm.group(1))
            pen = _Poly(None)
            top.CharStrings[gname].draw(pen)
            out[cid] = (widths.get(cid, dw) / 1000, _raster(pen.polys, scale))
        return out
    return None


def _pdf_widths(doc, dxref: int) -> dict[int, float]:
    """The /W array of a CID font: {CID: width in 1/1000 em}."""
    raw = doc.xref_get_key(dxref, "W")[1]
    if raw in ("null", ""):
        return {}
    toks = re.findall(r"\[|\]|[-\d.]+", raw)[1:-1]
    out, i = {}, 0
    while i < len(toks):
        first = int(float(toks[i]))
        if toks[i + 1] == "[":
            j = i + 2
            while toks[j] != "]":
                out[first + j - i - 2] = float(toks[j])
                j += 1
            i = j + 1
        else:
            last, w = int(float(toks[i + 1])), float(toks[i + 2])
            for c in range(first, last + 1):
                out[c] = w
            i += 3
    return out


def _choose_reference(glyphs: dict[int, tuple[float, frozenset]]) -> str:
    """The reference font that matches the glyphs best: average over the glyphs of the best drawing score among
    the reference characters of the same width (0 for a glyph whose width matches none)."""
    drawn = [(w, d) for w, d in glyphs.values() if d]

    def fit(name):
        ref = _reference(name)
        total = 0.0
        for w, d in drawn:
            pool = [r for r in ref if abs(r[1] - w) < 0.004]
            total += max((_score(d, r[2]) for r in pool), default=0.0)
        return total / max(1, len(drawn))
    return max(REFERENCES, key=fit)


def build_map(glyphs: dict[int, tuple[float, frozenset]], reference: str) -> dict[int, str] | None:
    """CID -> character, or None when the font does not look like the reference (a wrong letter is worse than a
    visible gap): most glyphs must match closely and few may be left unknown."""
    ref = _reference(reference)
    out: dict[int, str] = {}
    scores: list[float] = []
    small = sum(1 for _w, d in glyphs.values() if d) < 5  # no font-wide check possible: be stricter per glyph
    for cid, (width, drawing) in glyphs.items():
        if not drawing:
            out[cid] = " " if width > 0.1 else ""
            continue
        # The reference has the original's widths, so a character of the same width is strong evidence; the
        # drawing decides between them (digits all share one width). Without a width match only a close drawing
        # is accepted.
        same_width = [r for r in ref if abs(r[1] - width) < 0.004]
        pool, need = (same_width, 0.8 if small else 0.6) if same_width else (ref, 0.85)
        best = max(pool, key=lambda r: (round(_score(drawing, r[2]), 3), bool(GREEK.match(r[0]))))
        score = _score(drawing, best[2])
        scores.append(score)
        out[cid] = best[0] if score >= need else UNKNOWN
    if len(scores) >= 5:  # too few glyphs to judge the font as a whole: the per-glyph thresholds decide
        scores.sort()
        unknown = sum(1 for c in out.values() if c == UNKNOWN)
        if scores[len(scores) // 2] < 0.85 or unknown > 0.15 * len(scores):
            return None
    return out


def page_decoders(page, broken: set[str] | None = None) -> dict[str, dict[int, str]]:
    """{span font name: glyph map} for the fonts on this page that have no character map.
    `broken`: only these span font names (the fonts whose text came out as glyph numbers)."""
    if TTFont is None:
        return {}
    doc = page.parent
    cache = doc.__dict__.setdefault("_fekwatch_glyphmaps", {})
    out: dict[str, dict[int, str]] = {}
    for xref, ext, ftype, basefont, *_ in page.get_fonts(full=True):
        if ftype != "Type0" or doc.xref_get_key(xref, "ToUnicode")[0] != "null":
            continue
        if broken is not None and span_font_name(basefont) not in broken:
            continue
        if xref not in cache:
            try:
                glyphs = _embedded_glyphs(doc, xref)
                cache[xref] = build_map(glyphs, _choose_reference(glyphs)) if glyphs else None
            except Exception:  # noqa: BLE001 - a font that cannot be read is left undecoded
                cache[xref] = None
        if cache[xref]:
            out[span_font_name(basefont)] = cache[xref]
    return out


def span_font_name(basefont: str) -> str:
    """The name PyMuPDF gives a font in text spans: no subset prefix ("ABCDEF+"), no "-Identity-H" suffix."""
    return re.sub(r"-Identity-[HV]$", "", re.sub(r"^[A-Z]{6}\+", "", basefont))


def decode(text: str, glyphs: dict[int, str]) -> str:
    """Glyph numbers -> characters. Characters that are not glyph numbers of the font (the spaces PyMuPDF puts
    between words) are kept."""
    return "".join(glyphs[ord(c)] if ord(c) in glyphs else (c if ord(c) >= 32 else UNKNOWN) for c in text)
