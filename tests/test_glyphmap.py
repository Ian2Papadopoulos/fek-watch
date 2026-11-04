from fontTools.ttLib import TTFont

from fekwatch import glyphmap as g
from fekwatch.pdflinks import check_url, filename

TEXT = "Άρθρο 248 Παράρτημα - Δελτίο αναγνώρισης οικισμού: 2.000 κάτοικοι (ν. 4495/2017, άρθρο 30%)"


def _as_embedded(font_file: str, text: str):
    """Glyphs of a font as the decoder sees an embedded subset: {glyph id: (width in em, drawing)}."""
    font = TTFont(str(g.FONT_DIR / font_file))
    upem = font["head"].unitsPerEm
    cmap = font.getBestCmap()
    order = font.getGlyphOrder()
    gids = {}
    for ch in set(text):
        name = cmap[ord(ch)]
        gids[ch] = order.index(name)
    glyphs = {gid: (font["hmtx"][order[gid]][0] / upem, g._drawing(font, order[gid])) for gid in gids.values()}
    return glyphs, gids


def test_recovers_calibri_text():
    glyphs, gids = _as_embedded("Carlito-Regular.ttf", TEXT)
    assert g._choose_reference(glyphs) == "Calibri"
    m = g.build_map(glyphs, "Calibri")
    coded = "".join(chr(gids[ch]) for ch in TEXT)
    assert g.decode(coded, m) == TEXT


def test_arial_widths_choose_arimo():
    glyphs, gids = _as_embedded("Arimo-Bold.ttf", TEXT)
    assert g._choose_reference(glyphs) == "Arial Bold"
    m = g.build_map(glyphs, "Arial Bold")
    assert g.decode("".join(chr(gids[ch]) for ch in TEXT), m) == TEXT


def test_wrong_reference_is_refused():
    # Arial glyphs judged against Calibri: most widths differ, so the font is rejected instead of guessed
    glyphs, _ = _as_embedded("Arimo-Regular.ttf", TEXT)
    assert g.build_map(glyphs, "Calibri") is None


def test_spaces_between_words_are_kept():
    assert g.decode("\x01 \x02", {1: "Α", 2: "Β"}) == "Α Β"


def test_span_font_name():
    assert g.span_font_name("CIDFont+F1-Identity-H") == "CIDFont+F1"
    assert g.span_font_name("IKDXGI+Calibri-Bold") == "Calibri-Bold"


def test_pdf_links():
    url = "https://ia37rg02wpsa01.blob.core.windows.net/fek/01/2026/20260100122.pdf#page=33"
    assert check_url(url) == "01/2026/20260100122.pdf"
    assert filename(check_url(url)) == "FEK-A-122-2026.pdf"
    assert check_url("https://example.com/fek/01/2026/20260100122.pdf") is None
    assert check_url("https://ia37rg02wpsa01.blob.core.windows.net/fek/../etc/passwd") is None
