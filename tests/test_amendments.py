"""Amendment detection on real FEK text (fixtures extracted from the official PDFs)."""
import json
from pathlib import Path

from fekwatch.amendments import analyze, hits_for
from fekwatch.pdftext import ExtractedDoc

FIX = Path(__file__).parent / "fixtures"
W4495 = {"ν. 4495/2017": {"kind": "ν.", "number": "4495", "year": 2017, "aliases": []}}


def load(name) -> ExtractedDoc:
    return ExtractedDoc.from_json(json.loads((FIX / name).read_text(encoding="utf-8")))


def _hits(name, year, watched=W4495):
    an = analyze(load(name), year)
    return an, hits_for(an, watched)


def test_4546_2018_article_34_amendments():
    an, hits = _hits("A_101_2018_n4546_art34_45.json", 2018)
    assert an.act == "ν. 4546/2018"
    am = [h for h in hits if h.kind == "amendment" and h.provision.article == "34"]
    # 46 numbered paragraphs; §18's replacement text has an unterminated «, which used to swallow §19-§46
    assert [h.provision.label for h in am] == [str(i) for i in range(1, 47)]
    by_par = {h.provision.label: h.provision for h in am}
    assert by_par["2"].locator == {"article": "4", "paragraph": "γ", "case": "αα"}
    assert by_par["3"].locator == {"article": "7"} and by_par["3"].actions == ["replace"]
    assert by_par["9"].locator == {"article": "17", "paragraph": "1", "sentence": 2}
    # "13. Στο άρθρο 23 προστίθεται ..." does not repeat the law: inherited from the previous paragraph
    assert by_par["13"].target == "ν. 4495/2017" and by_par["13"].target_how == "inherited-title"


def test_items_numbered_with_parenthesis():
    """Art. 45 of ν. 4546/2018 numbers its items "1) 2) 3)"; each must be a separate provision."""
    an, hits = _hits("A_101_2018_n4546_art34_45.json", 2018,
                     {**W4495, "ν. 4067/2012": {"kind": "ν.", "number": "4067", "year": 2012, "aliases": []}})
    art45 = {(h.watched, h.provision.label): h for h in hits if h.provision.article == "45" and h.kind == "amendment"}
    assert {lab for (w, lab) in art45 if w == "ν. 4495/2017"} >= {"1", "2", "3", "4", "5", "7"}
    assert art45[("ν. 4067/2012", "6")].provision.locator["article"] == "23"


def test_mention_inside_quoted_text_is_not_an_amendment():
    """ν. 5313/2026 replaces references to 4495/2017 inside the Customs Code: not an amendment of 4495."""
    an, hits = _hits("A_102_2026_n5313_art9.json", 2026)
    assert hits and all(h.kind == "quoted" for h in hits)
    assert an.provisions[0].target == "ν. 5222/2025"


def test_ordinal_article_numbers():
    an, hits = _hits("A_191_2017_n4506_art7.json", 2017)
    am = [h for h in hits if h.kind == "amendment"]
    assert [(h.provision.article, h.provision.label) for h in am] == [("έβδομο", "1"), ("έβδομο", "2")]
    assert am[0].provision.locator == {"article": "29", "paragraph": "6"}


def _doc(paragraphs):
    from fekwatch.pdftext import Paragraph
    ps = [Paragraph(1, "ΝΟΜΟΣ ΥΠ’ ΑΡΙΘΜ. 9999 Δοκιμαστικός", bold=True)]
    for t in paragraphs:
        bold = t.startswith("Άρθρο ")
        ps.append(Paragraph(1, t, bold=bold, kind="heading" if bold else "body"))
    return ExtractedDoc(ps, 1)


def test_sunset_clause_of_amending_act_is_not_a_repeal():
    an = analyze(_doc(["Άρθρο 5 Μεταβατική", "1. Με την έκδοση του π.δ. της παρ. 2 του άρθρου 31 του ν. 4495/2017 "
                       "(Α’ 167), το παρόν άρθρο καταργείται."]), 2021)
    hits = hits_for(an, W4495)
    assert hits and all(h.kind != "amendment" for h in hits)


def test_citation_in_other_sentence_is_a_reference():
    an = analyze(_doc(["Άρθρο 5 Αιτήσεις", "1. Η αίτηση συνοδεύεται από βεβαίωση του άρθρου 107 του ν. 4495/2017. "
                       "Η προθεσμία του προηγούμενου εδαφίου τροποποιείται με απόφαση του Υπουργού."]), 2021)
    assert all(h.kind == "reference" for h in hits_for(an, W4495))


def test_history_clause_is_not_the_target():
    an = analyze(_doc(["Άρθρο 7 Τροποποιήσεις", "Στον ν. 4495/2017 επέρχονται οι εξής τροποποιήσεις:",
                       "1. Η παράγραφος 3 του άρθρου 26, όπως προστέθηκε με την παρ. 16 του άρθρου 34 του ν. 4546/2018 "
                       "(Α’ 101), αναριθμείται ως 3Α."]), 2019)
    p = [x for x in an.provisions if x.label == "1"][0]
    assert p.target == "ν. 4495/2017" and p.kind == "amendment"
