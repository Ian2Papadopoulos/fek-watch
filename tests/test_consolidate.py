"""Consolidation: correctness on real amendments, and the 'never guess' safety rules."""
import json
from pathlib import Path

import pytest

from fekwatch.amendments import analyze, hits_for
from fekwatch.config import watched_from
from fekwatch.consolidate import Law, Unit, build_law, consolidate_records, parse_units, render_html, split_sentences
from fekwatch.et_api import FekMeta
from fekwatch.pdftext import ExtractedDoc
from fekwatch.pipeline import make_record

FIX = Path(__file__).parent / "fixtures"
W = watched_from("ν. 4495/2017", "Έλεγχος και προστασία του Δομημένου Περιβάλλοντος")


def load(name) -> ExtractedDoc:
    return ExtractedDoc.from_json(json.loads((FIX / name).read_text(encoding="utf-8")))


def records(name, meta):
    an = analyze(load(name), meta.year)
    return [make_record(meta, an, h, W) for h in hits_for(an, {W.key: W.as_match()}) if h.kind == "amendment"]


@pytest.fixture(scope="module")
def result():
    law = build_law(W.key, load("A_167_2017_n4495_base_subset.json"), 2017)
    recs = records("A_191_2017_n4506_art7.json", FekMeta(1, 1, 191, 2017, "2017-12-12", "2017-12-12", 48))
    recs += records("A_101_2018_n4546_art34_45.json", FekMeta(2, 1, 101, 2018, "2018-06-01", "2018-06-01", 100))
    return consolidate_records(law, recs)


def _case(law, art, par, case):
    a = law.article(art)
    p = next(c for c in a.children if c.kind == "par" and c.label == par) if par else a
    return next(c for c in p.children if c.kind == "case" and c.label == case)


def test_only_failures_are_articles_outside_the_fixture(result):
    """The fixture holds a subset of the law's articles; every other amendment must apply."""
    st = result.stats()
    assert st["applied"] >= 40
    assert all(f.reason.startswith("δεν βρέθηκε το άρθρο") for f in result.failed), [f.reason for f in result.failed]


def test_verb_first_word_replacement():
    law = _law({"5": ("Τίτλος", ["1. Το ποσό είναι 100 ευρώ."])})
    r = _rec("1. Στο άρθρο 5 επέρχονται οι εξής τροποποιήσεις:\nα) στην παρ. 1 αντικαθίσταται ο αριθμός ⟦1⟧ με τον αριθμό ⟦2⟧.",
             {1: "100", 2: "200"}, {"article": "5"})
    res = consolidate_records(law, [r])
    assert not res.failed and law.article("5").children[0].text == "Το ποσό είναι 200 ευρώ."


def test_verb_first_repeal_only_hits_the_case(result):
    """'ε) Καταργείται η παράγραφος θ.' must repeal case θ of art. 25, not the whole article."""
    a25 = result.law.article("25")
    assert a25.status == "active"
    assert _case(result.law, "25", None, "θ").status == "repealed"
    assert _case(result.law, "25", None, "ι").status == "active"


def test_case_replaced_in_two_paragraphs(result):
    """'Η περίπτωση β’ της παραγράφου 1 και της παραγράφου 3 αντικαθίσταται ...'"""
    for par in ("1", "3"):
        c = _case(result.law, "14", par, "β")
        assert c.notes and c.text.startswith("γνωμοδοτεί επί των αρχιτεκτονικών μελετών της περίπτωσης ε’")
    assert _case(result.law, "14", "1", "δ").added  # new case δ’ (item ε of the amending paragraph)


def test_phrase_insertion_and_new_sentence(result):
    p6 = next(c for c in result.law.article("29").children if c.label == "6")
    assert "άδειας δόμησης, από την Υ.Π.Α. και μετά την έκδοση του προεδρικού διατάγματος" in p6.text
    assert split_sentences(p6.text)[-1].startswith("Για την έκδοση των οικοδομικών αδειών του προηγούμενου εδαφίου")


def test_second_sentence_after_case_list(result):
    p1 = next(c for c in result.law.article("17").children if c.label == "1")
    trailing = [c for c in p1.children if c.kind == "text"]
    assert trailing[0].text.startswith("Η Επιτροπή Εξέτασης Προσφυγών Αυθαιρέτων συγκροτείται με Απόφαση")
    assert trailing[0].history  # previous wording kept for verification


def test_provenance_in_html(result):
    html = render_html(W, result)
    assert "ΦΕΚ Α 101/2018" in html and "#page=" in html and "προηγούμενες διατυπώσεις" in html


# ---------------------------------------------------------------- safety rules on synthetic text

def _law(lines_by_article):
    arts = []
    for lab, (title, lines) in lines_by_article.items():
        kids, intro = parse_units(lines)
        arts.append(Unit("article", lab, title, intro, kids))
    return Law("ν. 9999/2020", "Δοκιμαστικός", arts)


def _rec(instruction, payloads, locator, n=1):
    return {"id": f"r{n}", "kind": "amendment", "article": "1", "par": str(n), "locator": locator,
            "instruction": instruction, "payloads": [{"id": i, "text": t, "page": 1} for i, t in payloads.items()],
            "source": {"label": "Α 1/2021", "number": 1, "issue_date": "2021-01-01", "act": "ν. 9998/2021",
                       "pdf": "x.pdf#page=1"}}


def test_subitem_without_locator_is_not_applied():
    law = _law({"5": ("Τίτλος", ["1. Πρώτη παράγραφος.", "2. Δεύτερη παράγραφος."])})
    r = _rec("1. Το άρθρο 5 τροποποιείται ως εξής:\nα) Αντικαθίσταται ως εξής: ⟦1⟧", {1: "Νέο κείμενο."}, {"article": "5"})
    res = consolidate_records(law, [r])
    assert res.stats()["applied"] == 0 and len(res.failed) == 1
    assert law.article("5").children[0].text == "Πρώτη παράγραφος."


def test_label_mismatch_is_rejected():
    law = _law({"5": ("Τίτλος", ["1. Εισαγωγή:", "α) πρώτη,", "β) δεύτερη,", "γ) τρίτη."])})
    r = _rec("1. Η περ. γ’ της παρ. 1 του άρθρου 5 αντικαθίσταται ως εξής: ⟦1⟧", {1: "δ) άσχετη."},
             {"article": "5", "paragraph": "1", "case": "γ"})
    res = consolidate_records(law, [r])
    assert res.failed and "αρχίζει με" in res.failed[0].reason


def test_missing_phrase_is_reported():
    law = _law({"5": ("Τίτλος", ["1. Το συμβούλιο είναι επταμελές."])})
    r = _rec("1. Στην παρ. 1 του άρθρου 5 η λέξη ⟦1⟧ αντικαθίσταται από τη λέξη ⟦2⟧.", {1: "πενταμελές", 2: "εννεαμελές"},
             {"article": "5", "paragraph": "1"})
    res = consolidate_records(law, [r])
    assert res.failed and "δεν βρέθηκε η φράση" in res.failed[0].reason


def test_word_replacement_applied():
    law = _law({"5": ("Τίτλος", ["1. Το συμβούλιο είναι επταμελές. Συνεδριάζει μηνιαίως."])})
    r = _rec("1. Στο πρώτο εδάφιο της παρ. 1 του άρθρου 5 η λέξη ⟦1⟧ αντικαθίσταται από τη λέξη ⟦2⟧.",
             {1: "επταμελές", 2: "εννεαμελές"}, {"article": "5", "paragraph": "1", "sentence": 1})
    res = consolidate_records(law, [r])
    assert not res.failed
    assert law.article("5").children[0].text == "Το συμβούλιο είναι εννεαμελές. Συνεδριάζει μηνιαίως."


def test_add_article_and_repeal_paragraph():
    law = _law({"5": ("Α", ["1. Ένα.", "2. Δύο."]), "6": ("Β", ["Κείμενο."])})
    recs = [
        _rec("1. Μετά το άρθρο 5 προστίθεται άρθρο 5Α ως εξής: ⟦1⟧", {1: "Άρθρο 5Α\nΝέος τίτλος\n1. Νέο."}, {"article": "5"}, 1),
        _rec("2. Η παρ. 2 του άρθρου 5 καταργείται.", {}, {"article": "5", "paragraph": "2"}, 2),
    ]
    res = consolidate_records(law, recs)
    assert not res.failed
    assert [a.label for a in law.articles] == ["5", "5Α", "6"]
    assert law.article("5Α").title == "Νέος τίτλος"
    assert law.article("5").children[1].status == "repealed"


def test_sentence_split_respects_abbreviations():
    s = "Η παρ. 5 του άρθρου 3 του ν. 4495/2017 ισχύει. Το π.δ. 18/2018 εφαρμόζεται. Τέλος."
    assert split_sentences(s) == ["Η παρ. 5 του άρθρου 3 του ν. 4495/2017 ισχύει.", "Το π.δ. 18/2018 εφαρμόζεται.", "Τέλος."]


def test_partial_restatement_with_article_heading_keeps_other_paragraphs():
    """'Τροποποιείται η παρ. 1 του άρθρου 89 ως εξής: «Άρθρο 89 / τίτλος / 1. ...»' must not drop παρ. 2."""
    law = _law({"89": ("Απαγόρευση", ["1. Παλιό πρώτο.", "2. Δεύτερο:", "α) πρώτη,", "β) δεύτερη."])})
    r = _rec("Τροποποιείται η παρ. 1 του άρθρου 89 ως εξής: ⟦1⟧", {1: "Άρθρο 89\nΑπαγόρευση\n1. Νέο πρώτο."},
             {"article": "89", "paragraph": "1"})
    res = consolidate_records(law, [r])
    assert not res.failed
    a = law.article("89")
    assert [c.label for c in a.children] == ["1", "2"]
    assert a.children[0].text == "Νέο πρώτο." and len(a.children[1].children) == 2


def test_restatement_supersedes_addition():
    """'... προστίθεται περ. γ’ και το άρθρο 30 διαμορφώνεται ως εξής: «Άρθρο 30 ...»' = replace the article."""
    law = _law({"30": ("Εργασίες", ["1. Δεν απαιτείται άδεια για:", "α) πρώτη,", "β) δεύτερη."])})
    r = _rec("Στην παρ. 1 του άρθρου 30 προστίθεται περ. γ’ και το άρθρο 30 διαμορφώνεται ως εξής: ⟦1⟧",
             {1: "Άρθρο 30\nΕργασίες\n1. Δεν απαιτείται άδεια για:\nα) πρώτη,\nβ) δεύτερη,\nγ) τρίτη."},
             {"article": "30", "paragraph": "1"})
    res = consolidate_records(law, [r])
    assert not res.failed
    p1 = law.article("30").children[0]
    assert [c.label for c in p1.children] == ["α", "β", "γ"]


def test_payload_paragraph_retargets_from_case():
    """'Η περ. ε) της παρ. 1 τροποποιείται ... «1. ...»': the new text is the whole παρ. 1, never nested in περ. ε."""
    law = _law({"39": ("Τίτλος", ["1. Περιλαμβάνει:", "α) ένα,", "ε) πέντε."])})
    r = _rec("Η περ. ε) της παρ. 1 του άρθρου 39 τροποποιείται ως εξής: ⟦1⟧", {1: "1. Περιλαμβάνει:\nα) ένα,\nε) πέντε νέο."},
             {"article": "39", "paragraph": "1", "case": "ε"})
    res = consolidate_records(law, [r])
    assert not res.failed
    p1 = law.article("39").children[0]
    assert [c.label for c in p1.children] == ["α", "ε"] and p1.children[1].text == "πέντε νέο."


# ---------------------------------------------------------------- restatement-first (ν. 5306/2026 cases)

def test_restated_article_wins_over_listed_changes():
    """'… α) … β) στο τέλος της παρ. 3 προστίθενται δύο νέα εδάφια, γ) προστίθεται παρ. 3Α και το άρθρο 199
    διαμορφώνεται ως εξής: «Άρθρο 199 …»' -> the FEK's own new wording is applied as a whole."""
    law = _law({"199": ("Χρήση κτιρίων", ["1. Πρώτη.", "2. Δεύτερη. Τρίτη.", "3. Τέταρτη."])})
    r = _rec("1. Στο άρθρο 199 επέρχονται οι ακόλουθες τροποποιήσεις: α) στο δεύτερο εδάφιο της παρ. 2 προστίθενται "
             "οι λέξεις ⟦1⟧, β) στο τέλος της παρ. 3 προστίθενται δύο νέα εδάφια, γ) προστίθεται παρ. 3Α και το "
             "άρθρο 199 διαμορφώνεται ως εξής: ⟦2⟧",
             {1: "εξαιρουμένων", 2: "Άρθρο 199\nΧρήση κτιρίων\n1. Πρώτη.\n2. Δεύτερη. Τρίτη εξαιρουμένων.\n"
                                    "3. Τέταρτη. Νέο ένα. Νέο δύο.\n3Α. Νέα παράγραφος."},
             {"article": "199"})
    res = consolidate_records(law, [r])
    assert not res.failed
    a = law.article("199")
    assert [c.label for c in a.children] == ["1", "2", "3", "3Α"]
    assert a.children[2].text == "Τέταρτη. Νέο ένα. Νέο δύο."


def test_partial_parse_is_not_applied_partially():
    """A listed step without quoted text must not be dropped silently: nothing is applied, it goes to review."""
    law = _law({"83": ("Τίτλος", ["1. Ένα.", "2. Δύο:", "α) πρώτη και 290,", "3. Τρία."])})
    r = _rec("1. Στο άρθρο 83 επέρχονται οι ακόλουθες τροποποιήσεις: α) στην περ. α) της παρ. 2, οι λέξεις ⟦1⟧ "
             "αντικαθίστανται από τις λέξεις ⟦2⟧, β) στην παρ. 3, προστίθεται νέο δεύτερο εδάφιο.",
             {1: "και 290", 2: ", 290 και 303"}, {"article": "83"})
    res = consolidate_records(law, [r])
    assert res.failed and law.article("83").children[1].children[0].text == "πρώτη και 290,"


def test_new_article_goes_after_its_base_number():
    law = Law("ν. 9999/2020", "x", [Unit("article", "πρώτο", "Κύρωση"), Unit("article", "198"),
                                    Unit("article", "199"), Unit("article", "200")])
    r = _rec("Στον Κώδικα προστίθεται άρθρο 199Α ως εξής: ⟦1⟧", {1: "Άρθρο 199Α\nΝέο\n1. Κείμενο."}, {})
    res = consolidate_records(law, [r])
    assert not res.failed
    assert [a.label for a in law.articles] == ["πρώτο", "198", "199", "199Α", "200"]


def test_headings_and_annex():
    from fekwatch.pdftext import Paragraph
    from fekwatch.amendments import segment
    P = lambda t, b=False: Paragraph(1, t, bold=b, kind="heading" if b else "body")  # noqa: E731
    doc = ExtractedDoc([P("ΝΟΜΟΣ ΥΠ’ ΑΡΙΘΜ. 9999 Δοκιμή", True), P("ΜΕΡΟΣ Α’ ΓΕΝΙΚΑ", True),
                        P("ΚΕΦΑΛΑΙΟ Α’ ΟΡΙΣΜΟΙ", True), P("Άρθρο 1 Ορισμοί", True), P("1. Κείμενο."),
                        P("ΚΕΦΑΛΑΙΟ Β’ ΛΟΙΠΑ", True), P("Άρθρο 2 Λοιπά", True), P("Κείμενο."),
                        P("ΠΑΡΑΡΤΗΜΑ Α’", True), P("Άρθρο 7", True), P("πίνακας"),
                        P("Άρθρο 3 Τελικό", True), P("Τέλος.")], 1)
    arts = [a for a in segment(doc, 2020)[0]["articles"] if a.label]
    assert [a.label for a in arts] == ["1", "2", "3"]          # "Άρθρο 7" is a table cell in the annex
    assert arts[0].headings == ["ΜΕΡΟΣ Α’ ΓΕΝΙΚΑ", "ΚΕΦΑΛΑΙΟ Α’ ΟΡΙΣΜΟΙ"] and arts[1].headings == ["ΚΕΦΑΛΑΙΟ Β’ ΛΟΙΠΑ"]
