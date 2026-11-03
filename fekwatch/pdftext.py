"""Extract clean, ordered paragraphs from a FEK PDF.

The National Printing Office PDFs are born-digital (not scanned) for every issue we care
about, so the text layer is reliable. What needs care is *structure*:

* pages are either two-column (older issues) or single full-width column (newer issues);
* every page carries a running header (gazette name, page number, issue label) and the
  first page a masthead plus a digital-signature box;
* the last page of each issue is the printing office's boilerplate.

We rebuild paragraphs from line geometry: a paragraph starts on an indented line or after
a line that ends short of the right margin; headings are recognised by their bold font.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import pymupdf

from .normalize import join_lines, normalize_chars

HEADER_ZONE_Y = 82.0  # running header lives above this y on every page
MIN_FONT_SIZE = 6.0  # signature boxes use ~4-5pt text

_BOILERPLATE_MARKERS = (
    "ΥΠΗΡΕΣΙΕΣ ΠΟΥ ΠΑΡΕΧΕΙ ΤΟ ΕΘΝΙΚΟ ΤΥΠΟΓΡΑΦΕΙΟ",
    "ΕΠΙΚΟΙΝΩΝΙΑ ΜΕ ΤΟ ΕΘΝΙΚΟ ΤΥΠΟΓΡΑΦΕΙΟ",
    "Καποδιστρίου 34",
)
_DROP_LINE_RES = [
    re.compile(r"^\*\d{8,}\*$"),  # barcode
    re.compile(r"^\(Το παρόν ΦΕΚ επανεκτυπώθηκε.*\)$"),
    re.compile(r"^(ΨΗΦΙΑΚΑ|ΥΠΟΓΕΓΡΑΜΜΕΝΟ|ΑΠΟ)$"),
    re.compile(r"^(Digitally signed by|Signature Not|Verified|Date: \d{4}\.\d\d\.\d\d)"),
]
_MASTHEAD_END_RE = re.compile(r"Αρ\.\s*Φύλλου\s*\d+")
_REPRINT_RE = re.compile(r"επανεκτυπώθηκε", re.I)
_HEADING_START_RE = re.compile(
    r"^(Άρθρο|ΑΡΘΡΟ|ΆΡΘΡΟ|ΜΕΡΟΣ|ΤΜΗΜΑ|ΚΕΦΑΛΑΙΟ|ΕΝΟΤΗΤΑ|ΤΙΤΛΟΣ|ΠΑΡΑΡΤΗΜΑ|ΥΠΟΚΕΦΑΛΑΙΟ)\b"
)


@dataclass
class Line:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    bold: bool
    size: float
    col: int = 0  # 0 = left/single, 1 = right, -1 = spans both columns


@dataclass
class Paragraph:
    page: int
    text: str
    bold: bool = False
    indent: bool = False
    kind: str = "body"  # body | heading | toc
    pages: list[int] = field(default_factory=list)


@dataclass
class ExtractedDoc:
    paragraphs: list[Paragraph]
    page_count: int
    reprinted: bool = False
    page_texts: dict[int, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "page_count": self.page_count,
            "reprinted": self.reprinted,
            "paragraphs": [
                {"page": p.page, "text": p.text, "bold": p.bold, "indent": p.indent,
                 "kind": p.kind, "pages": p.pages}
                for p in self.paragraphs
            ],
        }

    @classmethod
    def from_json(cls, d: dict) -> "ExtractedDoc":
        paras = [Paragraph(**p) for p in d["paragraphs"]]
        return cls(paragraphs=paras, page_count=d["page_count"], reprinted=d.get("reprinted", False))


def _is_bold(span: dict) -> bool:
    font = span.get("font", "")
    return bool(span.get("flags", 0) & 16) or any(k in font for k in ("Bold", "Semibold", "Black", "Heavy"))


def _page_lines(page: pymupdf.Page, pno: int) -> list[Line]:
    out: list[Line] = []
    # Exclude images: some issues embed huge scanned maps, and decoding them is very slow.
    d = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    for block in d["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = normalize_chars("".join(s["text"] for s in line["spans"])).strip()
            if not text:
                continue
            total = sum(len(s["text"].strip()) for s in spans) or 1
            bold_chars = sum(len(s["text"].strip()) for s in spans if _is_bold(s))
            size = max(s["size"] for s in spans)
            x0, y0, x1, y1 = line["bbox"]
            out.append(Line(pno, x0, y0, x1, y1, text, bold_chars / total > 0.6, size))
    return out


def _filter_lines(lines: list[Line], first_page: bool) -> list[Line]:
    keep = []
    masthead_end = None
    if first_page:
        for ln in lines:
            if _MASTHEAD_END_RE.search(ln.text):
                masthead_end = max(masthead_end or 0, ln.y1)
    for ln in lines:
        if ln.size < MIN_FONT_SIZE:
            continue
        if ln.y0 < HEADER_ZONE_Y:
            continue
        if masthead_end is not None and ln.y0 <= masthead_end + 2:
            continue
        if any(r.search(ln.text) for r in _DROP_LINE_RES):
            continue
        keep.append(ln)
    return keep


def _order_lines(lines: list[Line], width: float) -> list[Line]:
    """Order lines for reading: bands separated by full-width lines, left column first."""
    if not lines:
        return lines
    mid = width / 2
    for ln in lines:
        if ln.x0 < mid - 15 and ln.x1 > mid + 15:
            ln.col = -1
        elif ln.x0 >= mid - 15:
            ln.col = 1
        else:
            ln.col = 0
    spanning = sum(1 for ln in lines if ln.col == -1)
    if spanning / len(lines) > 0.35:
        # Single-column page: everything is one column in y order.
        for ln in lines:
            ln.col = 0
        return sorted(lines, key=lambda l: (round(l.y0, 1), l.x0))
    seps = sorted((ln for ln in lines if ln.col == -1), key=lambda l: l.y0)
    ordered: list[Line] = []
    prev_y = -1.0
    for sep in seps + [None]:
        top = sep.y0 if sep is not None else 10_000.0
        band = [ln for ln in lines if ln.col != -1 and prev_y <= ln.y0 < top]
        ordered += sorted((l for l in band if l.col == 0), key=lambda l: (l.y0, l.x0))
        ordered += sorted((l for l in band if l.col == 1), key=lambda l: (l.y0, l.x0))
        if sep is not None:
            ordered.append(sep)
            prev_y = sep.y0 + 0.01
    return ordered


def _margins(lines: list[Line]) -> dict[int, tuple[float, float]]:
    """Per-column (left margin, right margin) estimated from body lines."""
    res = {}
    for col in (-1, 0, 1):
        xs = [l for l in lines if l.col == col and not l.bold]
        if not xs:
            xs = [l for l in lines if l.col == col]
        if not xs:
            continue
        x0s = sorted(l.x0 for l in xs)
        x1s = sorted(l.x1 for l in xs)
        left = x0s[max(0, int(len(x0s) * 0.1) - 1)] if len(x0s) > 3 else min(x0s)
        right = x1s[int(len(x1s) * 0.9)] if len(x1s) > 3 else max(x1s)
        res[col] = (left, right)
    return res


def extract_pdf(path: str | Path) -> ExtractedDoc:
    doc = pymupdf.open(str(path))
    all_lines: list[Line] = []
    margins_by_page: dict[int, dict[int, tuple[float, float]]] = {}
    reprinted = False
    page_texts: dict[int, str] = {}
    for i, page in enumerate(doc):
        pno = i + 1
        raw = _page_lines(page, pno)
        joined = " ".join(l.text for l in raw)
        if any(m in joined for m in _BOILERPLATE_MARKERS) and pno > 1:
            continue
        if _REPRINT_RE.search(joined):
            reprinted = True
        lines = _filter_lines(raw, first_page=(pno == 1))
        lines = _order_lines(lines, page.rect.width)
        margins_by_page[pno] = _margins(lines)
        page_texts[pno] = "\n".join(l.text for l in lines)
        all_lines += lines
    page_count = doc.page_count
    doc.close()
    paras = _build_paragraphs(all_lines, margins_by_page)
    _mark_toc(paras)
    return ExtractedDoc(paragraphs=paras, page_count=page_count, reprinted=reprinted, page_texts=page_texts)


def _build_paragraphs(lines: list[Line], margins_by_page) -> list[Paragraph]:
    paras: list[Paragraph] = []
    cur: list[Line] = []

    def flush():
        if not cur:
            return
        text = join_lines([l.text for l in cur])
        if text:
            first = cur[0]
            m = margins_by_page.get(first.page, {}).get(first.col)
            indent = bool(m and first.x0 > m[0] + 4)
            pages = sorted({l.page for l in cur})
            bold = all(l.bold for l in cur)
            kind = "heading" if bold else "body"
            paras.append(Paragraph(first.page, text, bold, indent, kind, pages))
        cur.clear()

    heights = [l.y1 - l.y0 for l in lines if not l.bold]
    lh = median(heights) if heights else 11.0
    prev: Line | None = None
    for ln in lines:
        if prev is None:
            cur.append(ln)
            prev = ln
            continue
        m = margins_by_page.get(ln.page, {}).get(ln.col) or (ln.x0, ln.x1)
        pm = margins_by_page.get(prev.page, {}).get(prev.col) or (prev.x0, prev.x1)
        same_flow = prev.page == ln.page and prev.col == ln.col
        gap = ln.y0 - prev.y1 if same_flow else 0.0
        new = False
        if ln.bold != prev.bold:
            new = True
        elif ln.bold:
            # Inside a run of bold lines: new heading when a heading keyword starts,
            # or when there is a visible vertical gap.
            if _HEADING_START_RE.match(ln.text) or gap > lh * 0.9:
                new = True
        else:
            indented = ln.x0 > m[0] + 4
            prev_short = prev.x1 < pm[1] - 15
            if indented or prev_short or gap > lh * 1.2:
                new = True
        if new:
            flush()
        cur.append(ln)
        prev = ln
    flush()
    return paras


def _mark_toc(paras: list[Paragraph]) -> None:
    """Mark the table of contents (post-2019 laws) so it is not parsed as the body."""
    start = None
    for i, p in enumerate(paras[:60]):
        if p.text.startswith("ΠΙΝΑΚΑΣ ΠΕΡΙΕΧΟΜΕΝΩΝ"):
            start = i
            break
    if start is None:
        return
    # The TOC ends at the first bold heading after it.
    for j in range(start + 1, len(paras)):
        if paras[j].bold and _HEADING_START_RE.match(paras[j].text):
            for k in range(start, j):
                paras[k].kind = "toc"
            return


def paragraphs_text(doc: ExtractedDoc, include_toc: bool = False) -> str:
    return "\n".join(p.text for p in doc.paragraphs if include_toc or p.kind != "toc")
