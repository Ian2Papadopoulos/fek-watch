"""Unit tests for normalisation and citation/locator parsing."""
from fekwatch.citations import (find_act_refs, find_actions, find_mentions, greek_letter_value, ordinal_value,
                                parse_act_key, parse_locator)
from fekwatch.normalize import fold, join_lines, normalize_chars


def test_homoglyphs_and_symbols():
    assert normalize_chars("NOMOΣ ΥΠ’ ΑΡΙΘΜ. 4495") == "ΝΟΜΟΣ ΥΠ’ ΑΡΙΘΜ. 4495"
    assert normalize_chars("ΕΦΗΜΕΡΙ∆Α TΗΣ ΚΥΒΕΡΝΗΣΕΩΣ") == "ΕΦΗΜΕΡΙΔΑ ΤΗΣ ΚΥΒΕΡΝΗΣΕΩΣ"
    assert normalize_chars("Τεύχος A’ 102/25.06.2026") == "Τεύχος Α’ 102/25.06.2026"
    assert normalize_chars("To άρθρο 7") == "Το άρθρο 7"
    assert normalize_chars("(Α΄ 167)") == normalize_chars("(Α' 167)") == "(Α’ 167)"


def test_dehyphenation():
    assert join_lines(["Έλεγχος και προστασία του Δομημένου Περιβάλ-", "λοντος και άλλες"]) == \
        "Έλεγχος και προστασία του Δομημένου Περιβάλλοντος και άλλες"
    assert join_lines(["Κώδικα Χωροταξίας-", "Πολεοδομίας"]) == "Κώδικα Χωροταξίας-Πολεοδομίας"
    assert join_lines(["Ελέγχου Δόμησης -", "Τοπικά"]) == "Ελέγχου Δόμησης - Τοπικά"


def test_fold_keeps_offsets():
    s = "Η παράγραφος 5 του άρθρου 3 αντικαθίσταται ως εξής:"
    assert len(fold(s)) == len(s)
    assert "αντικαθισταται" in fold(s)


def test_act_refs():
    t = normalize_chars("των ν. 4067/2012 (Α’ 79) και 4495/2017 (Α’ 167) και του π.δ. 24/31.5.1985 (Δ’ 270)")
    refs = [(r.key, r.fek) for r in find_act_refs(t)]
    assert refs == [("ν. 4067/2012", "Α 79"), ("ν. 4495/2017", "Α 167"), ("π.δ. 24/1985", "Δ 270")]
    t = normalize_chars("του Εθνικού Τελωνειακού Κώδικα (ν. 5222/2025, Α’ 134)")
    assert [(r.key, r.fek) for r in find_act_refs(t)] == [("ν. 5222/2025", "Α 134")]
    assert [r.key for r in find_act_refs("του ν. 4495/ 2017, αντικαθίσταται")] == ["ν. 4495/2017"]
    assert parse_act_key("4067/12") == ("ν.", "4067", 2012)


def test_mentions_exclude_decision_numbers():
    assert find_mentions("σύμφωνα με την ΥΠΕΝ/ΔΑΟΚΑ/4495/2017 απόφαση", "ν.", "4495", 2017) == []
    assert find_mentions("του ν. 4495/2017 (Α’ 167)", "ν.", "4495", 2017)[0][2] == 1.0


def test_locators():
    loc = parse_locator("Η περίπτωση γ’ της παρ. 1 του άρθρου 8 του ν. 4495/2017")
    assert (loc.article, loc.paragraph, loc.case) == ("8", "1", "γ")
    loc = parse_locator("Οι παρ. 2 και 3 του άρθρου 23Α")
    assert (loc.article, loc.paragraph, loc.extra_paragraphs) == ("23Α", "2", ["3"])
    loc = parse_locator("Το δεύτερο εδάφιο της παρ. 6 του άρθρου 29")
    assert (loc.article, loc.paragraph, loc.sentence) == ("29", "6", 2)
    loc = parse_locator("Η υποπερ. ββ’ της περ. β’ της παρ. 3 του άρθρου 99")
    assert (loc.article, loc.paragraph, loc.case, loc.subcase) == ("99", "3", "β", "ββ")
    assert parse_locator("Το άρθρο έβδομο του ν. 4508/2017").article == "7"
    assert parse_locator("Οι παρ. 2 έως 5 του άρθρου 5").extra_paragraphs == ["3", "4", "5"]
    assert parse_locator("Η περίπτωση β’ της παραγράφου 1 και της παραγράφου 3").extra_paragraphs == ["3"]


def test_ordinals_and_letters():
    assert ordinal_value("δεύτερο") == 2 and ordinal_value("τελευταίο") == -1 and ordinal_value("εικοστό πρώτο") == 21
    assert greek_letter_value("στ") == 6 and greek_letter_value("ιβ") == 12 and greek_letter_value("αα") == 1
    assert greek_letter_value("ωσ") is None


def test_actions():
    assert [a[0] for a in find_actions(fold("επέρχονται οι ακόλουθες τροποποιήσεις"))] == ["modify"]
    assert [a[0] for a in find_actions(fold("καταργούνται"))] == ["repeal"]
