"""Turn an extracted FEK into amending provisions.

Pipeline for one gazette issue:

1. split the issue into acts (an Α' issue can carry a law plus decrees or corrections);
2. split each act into its own articles, using the bold "Άρθρο N" headings — headings that
   appear inside quoted replacement text are set in regular weight, so they are not
   mistaken for articles of the amending act;
3. inside each article, track «…» quote depth: quoted text (the new wording being
   inserted) becomes a numbered *payload* and is replaced by a ⟦n⟧ placeholder, leaving a
   short *instruction* text ("Η παρ. 6 του άρθρου 29 του ν. 4495/2017 αντικαθίσταται ως
   εξής: ⟦3⟧");
4. split the instruction text into numbered provisions and resolve, for each one, the
   target act, the structural locator and the action verbs.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict

from .citations import (
    ACTION_PATTERNS, DEROGATION_RE, HISTORY_CLAUSE_RE, ActRef, Locator, find_act_refs,
    find_actions, find_mentions, parse_act_key, parse_locator,
)
from .normalize import fold
from .pdftext import ExtractedDoc, Paragraph

OPEN_Q, CLOSE_Q = "«", "»"
PH_RE = re.compile(r"⟦(\d+)⟧")

_ACT_HEADER_RE = re.compile(
    r"^(?P<kind>ΝΟΜΟΣ|ΠΡΟΕΔΡΙΚΟ ΔΙΑΤΑΓΜΑ|ΠΡΑΞΗ ΝΟΜΟΘΕΤΙΚΟΥ ΠΕΡΙΕΧΟΜΕΝΟΥ|ΝΟΜΟΘΕΤΙΚΟ ΔΙΑΤΑΓΜΑ)"
    r"(?:\s+ΥΠ’?\s*ΑΡΙΘΜ\.?\s*(?P<num>\d+))?\s*(?P<rest>.*)$"
)
_CORRECTION_HEADER_RE = re.compile(r"^ΔΙΟΡΘΩΣΕΙΣ ΣΦΑΛΜΑΤΩΝ|^Διόρθωση σφάλματος|^Διόρθωση σφαλμάτων", re.I)
_ARTICLE_RE = re.compile(r"^(?:Άρθρο|ΑΡΘΡΟ|ΆΡΘΡΟ)\s+(?P<label>\d+[Α-Ω]{0,3}(?![α-ωά-ώ])|[Α-Ωα-ωάέήίόύώ]+(?:\s[α-ωάέήίόύώ]+)?)\s*(?P<title>.*)$")
_SECTION_RE = re.compile(r"^(ΜΕΡΟΣ|ΤΜΗΜΑ|ΚΕΦΑΛΑΙΟ|ΥΠΟΚΕΦΑΛΑΙΟ|ΕΝΟΤΗΤΑ)\b")
_ANNEX_RE = re.compile(r"^ΠΑΡΑΡΤΗΜΑ(?:ΤΑ)?\b")
_TITLE_AMEND_RE = re.compile(r"^(τροποποιησ\w*|συμπληρωσ\w*|αντικαταστασ\w*|καταργησ\w*)\b")
_TITLE_ACTIONS = {"τροποπ": "modify", "συμπλη": "add", "αντικα": "replace", "καταργ": "repeal"}
_TITLE_TAIL_RE = re.compile(r"\s*(?:Ο|Η|ΟΙ)\s+(?:ΑΝΑΠΛΗΡΩΤ\w+\s+)?(?:ΥΦΥΠΟΥΡΓΟΣ|ΥΠΟΥΡΓΟΣ|ΥΠΟΥΡΓΟΙ|ΠΡΟΕΔΡΟΣ|ΓΕΝΙΚΟΣ)\b.*$")
# Each decision in a Β' issue starts with its number: "Αριθμ. ΥΠΕΝ/ΔΑΟΚΑ/66006/2360"
_DECISION_HEADER_RE = re.compile(r"^(?:Αριθμ|Αριθ|ΑΡΙΘΜ)\.?\s*(?:οικ\.?\s*)?(?P<proto>[^\s]*\d[^\s]*)\s*$")
# An article heading on a line of its own, used when headings are not set in bold.
_STANDALONE_ARTICLE_RE = re.compile(
    r"^(?:Άρθρο|ΑΡΘΡΟ|ΆΡΘΡΟ)\s+(?P<label>\d{1,4}[Α-Ω]{0,3}|[Α-Ωα-ωάέήίόύώΆΈΉΊΌΎΏ][α-ωάέήίόύώ]+(?:\s[α-ωάέήίόύώ]+)?)\s*[:.]?\s*$")


_LEVEL = {"ΜΕΡΟΣ": 0, "ΤΜΗΜΑ": 1, "ΚΕΦΑΛΑΙΟ": 2, "ΥΠΟΚΕΦΑΛΑΙΟ": 3, "ΕΝΟΤΗΤΑ": 4}


def heading_level(text: str) -> int:
    return _LEVEL.get(text.split()[0] if text else "", 4)


def _heading_path(heads: list[str]) -> list[str]:
    """Keep only the headings in effect at the article (a later ΚΕΦΑΛΑΙΟ replaces an earlier one)."""
    path: list[str] = []
    for h in heads:
        lv = heading_level(h)
        path = [x for x in path if heading_level(x) < lv] + [h]
    return path


def _article_num(label: str) -> int | None:
    from .citations import ordinal_value
    m = re.match(r"^(\d+)", label or "")
    if m:
        return int(m.group(1))
    return ordinal_value(label) if label else None


def _looks_like_title(text: str) -> bool:
    return (len(text) < 220 and not text.rstrip().endswith((".", ":", ";", "»."))
            and not re.match(r"^\d+\.\d+", text)  # "5.1. Στόχος" is a sub-heading of the body
            and not re.match(r"^(\d{1,3}[α-ω]?[.)]|[α-ω]{1,3}\))\s", text) and OPEN_Q not in text[:1])
_NUM_PAR_RE = re.compile(r"(?m)^(?P<n>\d{1,3}[α-ω]?)(?:\.|\))\s?(?=\S)")
_SELF_REF_RE = re.compile(r"του παροντοσ|τησ παρουσασ|του ιδιου του παροντοσ")
_SELF_SUBJECT_RE = re.compile(r"(?:^|[\s,])(?:το παρον|η παρουσα|ο παρων|τα παροντα|οι παρουσεσ)\b[^,]{0,40}$")
_SAME_LAW_RE = re.compile(r"του (?:ιδιου|ως ανω|ανωτερω|αυτου) (?:νομου|διαταγματοσ)|του νομου αυτου")
_KIND_MAP = {"ΝΟΜΟΣ": "ν.", "ΠΡΟΕΔΡΙΚΟ ΔΙΑΤΑΓΜΑ": "π.δ.", "ΠΡΑΞΗ ΝΟΜΟΘΕΤΙΚΟΥ ΠΕΡΙΕΧΟΜΕΝΟΥ": "π.ν.π.",
             "ΝΟΜΟΘΕΤΙΚΟ ΔΙΑΤΑΓΜΑ": "ν.δ."}


@dataclass
class Payload:
    id: int
    text: str
    page: int


@dataclass
class Provision:
    act: str                 # amending act, e.g. "ν. 4546/2018"
    article: str             # article label of the amending act, e.g. "34"
    article_title: str
    label: str               # numbered paragraph of the amending article ("" if none)
    page: int
    instruction: str         # text outside quotes, quotes replaced by ⟦n⟧
    payload_ids: list[int]
    target: str | None       # act key the provision acts on
    target_how: str          # explicit | inherited-lead | inherited-title | same-law | alias | none
    locator: dict
    actions: list[str]
    kind: str                # amendment | derogation | reference | quoted
    cited: list[str]         # acts cited outside quotes
    quoted_cited: list[str]  # acts cited only inside quoted text
    articles: list[str] = field(default_factory=list)  # articles of the target affected (incl. new ones)
    whole_act: bool = False  # the act as a whole amends the target (e.g. "Τροποποίηση της … απόφασης")

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ActArticle:
    label: str
    title: str
    page: int
    section: str
    paras: list[Paragraph] = field(default_factory=list)
    headings: list[str] = field(default_factory=list)  # ΜΕΡΟΣ/ΤΜΗΜΑ/ΚΕΦΑΛΑΙΟ… since the previous article
    annex: bool = False


@dataclass
class ActAnalysis:
    act: str | None          # "ν. 4546/2018" (main act in the issue)
    act_title: str
    acts: list[str]
    articles: list[ActArticle]
    provisions: list[Provision]
    payloads: dict[int, Payload]

    def payload_text(self, pid: int) -> str:
        p = self.payloads.get(pid)
        return p.text if p else ""

    def render(self, prov: Provision, max_payload: int | None = None) -> str:
        """Instruction text with payloads put back in «…» (optionally truncated)."""
        def rep(m):
            t = self.payload_text(int(m.group(1)))
            if max_payload and len(t) > max_payload:
                t = t[:max_payload].rstrip() + " …"
            return OPEN_Q + t.replace("\n", " ") + CLOSE_Q
        return PH_RE.sub(rep, prov.instruction)


# --------------------------------------------------------------------------- segmentation

def _header(paragraph_text: str):
    m = _ACT_HEADER_RE.match(paragraph_text)
    if not m:
        return None
    return m


def segment(doc: ExtractedDoc, year: int | None = None, issue_date: str | None = None):
    """Yield (act_key, act_title, articles) for each act in the issue."""
    paras = [p for p in doc.paragraphs if p.kind != "toc"]
    acts = []
    cur_act = {"key": None, "title": "", "articles": [], "section": "", "heads": [], "annex": False}
    cur_art: ActArticle | None = None

    def start_act(key, title):
        nonlocal cur_act, cur_art
        if cur_act["articles"] or cur_act["key"]:
            acts.append(cur_act)
        cur_act = {"key": key, "title": title, "articles": [], "section": "", "heads": [], "annex": False}
        cur_art = None

    def new_article(label, title, page):
        nonlocal cur_art
        heads = cur_act["heads"]
        if len(heads) > 12:
            heads = []  # a run of bare headings with no articles in between is a table of contents
        cur_art = ActArticle(label, title, page, cur_act["section"], headings=_heading_path(heads))
        cur_act["heads"] = []
        cur_act["articles"].append(cur_art)

    def start_annex(text, page):
        nonlocal cur_art
        cur_act["annex"] = True
        cur_art = ActArticle("", text, page, cur_act["section"], annex=True)
        cur_act["articles"].append(cur_art)

    depth = 0              # «» depth, so headings inside quoted text are ignored
    last_num: int | None = None
    ord_last = 0           # last outer article written as a word (πρώτο, δεύτερο, ...)
    pending_title = False  # collecting title lines after a regular-weight heading
    for p in paras:
        text = p.text
        if p.bold:
            hm = _header(text)
            if hm and (hm.group("num") or hm.group("kind") == "ΠΡΑΞΗ ΝΟΜΟΘΕΤΙΚΟΥ ΠΕΡΙΕΧΟΜΕΝΟΥ"):
                kind = _KIND_MAP[hm.group("kind")]
                key = f"{kind} {int(hm.group('num'))}/{year}" if hm.group("num") and year else None
                if kind == "π.ν.π." and issue_date:
                    y_, m_, d_ = issue_date.split("-")
                    key = f"π.ν.π. {int(d_)}.{int(m_)}/{y_}"
                title = re.sub(r"\s*Ο ΠΡΟΕΔΡΟΣ ΤΗΣ ΕΛΛΗΝΙΚΗΣ ΔΗΜΟΚΡΑΤΙΑΣ.*$", "", hm.group("rest")).strip()
                start_act(key, title)
                depth, last_num, pending_title = 0, None, False
                continue
            dm = _DECISION_HEADER_RE.match(text)
            if dm and depth == 0 and (cur_act["key"] is None or cur_act["key"].startswith("απόφ.")):
                start_act(f"απόφ. {dm.group('proto').rstrip('.,')}", "")
                depth, last_num, pending_title = 0, None, False
                continue
            if cur_act["key"] and cur_act["key"].startswith("απόφ.") and not cur_act["title"] \
                    and not cur_act["articles"] and not _ARTICLE_RE.match(text):
                cur_act["title"] = text  # the decision's subject line follows its number
                continue
            if _CORRECTION_HEADER_RE.match(text):
                start_act(None, text)
                cur_act["title"] = text
                depth, last_num, pending_title = 0, None, False
                continue
            if _ANNEX_RE.match(text) and depth == 0 and cur_act["articles"]:
                start_annex(text, p.page)
                pending_title = False
                continue
            am = _ARTICLE_RE.match(text)
            if am and cur_act["annex"]:
                # an annex ends where the act's own numbering continues ("Άρθρο 429 <title>" after 428);
                # bare "Άρθρο 27" cells of correspondence tables stay inside the annex
                n = _article_num(am.group("label").strip())
                seen = {a.label for a in cur_act["articles"] if a.label}
                if n is not None and last_num is not None and n == last_num + 1 and am.group("label").strip() not in seen:
                    cur_act["annex"] = False
            if am and not text.startswith(OPEN_Q) and not cur_act["annex"]:
                new_article(am.group("label").strip(), am.group("title").strip(), p.page)
                depth, pending_title = 0, False  # a real heading: any open quote was unbalanced
                last_num = _article_num(cur_art.label) or last_num
                continue
            if _SECTION_RE.match(text) and not cur_act["annex"]:
                cur_act["section"] = text
                cur_act["heads"].append(text)
                pending_title = False
                continue
            if cur_art is not None and not cur_art.paras and not cur_art.annex:
                # continuation of a multi-paragraph article title
                cur_art.title = (cur_art.title + " " + text).strip()
                continue
        elif depth == 0:
            dm = _DECISION_HEADER_RE.match(text)
            if dm and (cur_act["key"] is None or cur_act["key"].startswith("απόφ.")):
                start_act(f"απόφ. {dm.group('proto').rstrip('.,')}", "")
                depth, last_num, pending_title = 0, None, False
                continue
            sm = _STANDALONE_ARTICLE_RE.match(text)
            n = _article_num(sm.group("label")) if sm else None
            if _ANNEX_RE.match(text) and text.upper() == text and len(text) < 300 and cur_act["articles"] \
                    and any(a.label for a in cur_act["articles"]):
                start_annex(text, p.page)
                pending_title = False
                continue
            if sm:
                lab = sm.group("label").strip()
                is_word = not lab[0].isdigit()
                in_seq = last_num is None or n in (last_num + 1, last_num)
                restart = n == 1 and bool(cur_act["heads"])       # numbering restarts in a new chapter
                outer = is_word and n is not None and n == ord_last + 1  # "Άρθρο Πρώτο" -> "Άρθρο Δεύτερο"
                if cur_act["annex"] and outer:
                    cur_act["annex"] = False
                if not cur_act["annex"] and (in_seq or restart or outer):
                    new_article(lab, "", p.page)
                    if is_word:
                        ord_last, last_num = n or ord_last, None
                    else:
                        last_num = n if n is not None else last_num
                    pending_title = True
                    continue
            if pending_title and cur_art is not None and not cur_art.paras and _looks_like_title(text):
                cur_art.title = (cur_art.title + " " + text).strip()
                continue
            if _SECTION_RE.match(text) and text.upper() == text and len(text) < 300 and not cur_act["annex"]:
                cur_act["section"] = text
                cur_act["heads"].append(text)
                pending_title = False
                continue
        pending_title = False
        if cur_art is None:
            cur_art = ActArticle("", "", p.page, cur_act["section"])
            cur_act["articles"].append(cur_art)
        cur_art.paras.append(p)
        depth = max(0, depth + text.count(OPEN_Q) - text.count(CLOSE_Q))
    acts.append(cur_act)
    return [a for a in acts if a["articles"]]


# --------------------------------------------------------------------------- quotes

def _split_quotes(paras: list[Paragraph], payloads: dict[int, Payload], next_id: list[int]):
    """Return (instruction_text, line_pages) for an article, filling `payloads`.

    Instruction text keeps one line per paragraph that has depth-0 content."""
    depth = 0
    instr_lines: list[str] = []
    line_pages: list[int] = []
    cur_payload: list[str] | None = None
    cur_pid = None
    last_num: int | None = None
    for p in paras:
        buf = []
        m = _NUM_PAR_RE.match(p.text)
        if m:
            n = int(re.match(r"\d+", m.group("n")).group())
            if (depth > 0 and last_num is not None and n == last_num + 1 and find_actions(fold(p.text[:400]))
                    and CLOSE_Q not in p.text and find_act_refs(p.text[:400])):
                # Resync: we are "inside quotes" but this paragraph is clearly the next provision.
                # The FEK has an unterminated « (a common drafting slip); close it here.
                if cur_pid is not None and cur_payload is not None:
                    payloads[cur_pid].text = "".join(cur_payload).strip() + " [?]"
                depth, cur_payload, cur_pid = 0, None, None
            if depth == 0:
                last_num = n
        for ch in p.text:
            if ch == OPEN_Q:
                if depth == 0:
                    cur_pid = next_id[0]
                    next_id[0] += 1
                    cur_payload = []
                    payloads[cur_pid] = Payload(cur_pid, "", p.page)
                    buf.append(f"⟦{cur_pid}⟧")
                else:
                    cur_payload.append(ch)
                depth += 1
                continue
            if ch == CLOSE_Q and depth > 0:
                depth -= 1
                if depth == 0:
                    payloads[cur_pid].text = "".join(cur_payload).strip()
                    cur_payload = None
                else:
                    cur_payload.append(ch)
                continue
            if depth == 0:
                buf.append(ch)
            else:
                cur_payload.append(ch)
        if depth > 0 and cur_payload is not None:
            cur_payload.append("\n")
        line = "".join(buf).strip()
        if line:
            instr_lines.append(line)
            line_pages.append(p.page)
    if depth > 0 and cur_payload is not None and cur_pid is not None:
        # Unbalanced quotes: keep what we have, flagged by a trailing marker.
        payloads[cur_pid].text = "".join(cur_payload).strip() + " [?]"
    return instr_lines, line_pages


def _split_provisions(lines: list[str], pages: list[int]) -> list[tuple[str, str, int]]:
    """Group instruction lines into numbered provisions: [(label, text, page)]."""
    out: list[tuple[str, list[str], int]] = []
    last: int | None = None
    for line, page in zip(lines, pages):
        m = _NUM_PAR_RE.match(line)
        n = int(re.match(r"\d+", m.group("n")).group()) if m else None
        # Only a continuation of the numbering starts a new provision: a stray "9." printed
        # outside the quotes of a replacement text must not become a provision of its own.
        if m and (last is None or n == last + 1 or (n == last and not m.group("n").isdigit())):
            out.append((m.group("n"), [line], page))
            last = n
        elif out:
            out[-1][1].append(line)
        else:
            out.append(("", [line], page))
    return [(lab, "\n".join(ls), pg) for lab, ls, pg in out]


# --------------------------------------------------------------------------- resolution

_ADD_ART_RE = re.compile(r"προστιθ(?:εται|ενται)\s+(?:νε(?:ο|α)\s+)?αρθρ(?:ο|α)\s+(\d{1,4}[α-ω]{0,3})"
                         r"(?:\s*(?:,|και|εωσ|-)\s*(\d{1,4}[α-ω]{0,3}))*")
_GREEK_SEQ = "α β γ δ ε στ ζ η θ ι ια ιβ ιγ ιδ ιε ιστ ιζ ιη ιθ κ κα κβ κγ κδ κε".split()


def _added_articles(f: str, verb_pos: int) -> list[str]:
    """New article labels in "προστίθεται άρθρο 25Α" / "προστίθενται άρθρα 125Α έως 125Κ"."""
    m = _ADD_ART_RE.search(f, max(0, verb_pos - 5))
    if not m:
        return []
    labels = re.findall(r"\d{1,4}[α-ω]{0,3}", m.group(0))
    if "εωσ" in m.group(0) and len(labels) == 2:
        a, b = labels
        ma, mb = re.match(r"(\d+)([α-ω]*)", a), re.match(r"(\d+)([α-ω]*)", b)
        if ma.group(1) == mb.group(1) and ma.group(2) in _GREEK_SEQ and mb.group(2) in _GREEK_SEQ:
            i, j = _GREEK_SEQ.index(ma.group(2)), _GREEK_SEQ.index(mb.group(2))
            labels = [ma.group(1) + x for x in _GREEK_SEQ[i: j + 1]]
        elif ma.group(2) == mb.group(2) == "":
            labels = [str(k) for k in range(int(ma.group(1)), int(mb.group(1)) + 1)]
    return [l.upper() for l in labels]


def _first_verb_pos(f: str) -> int:
    acts = find_actions(f)
    d = DEROGATION_RE.search(f)
    cands = [a[1] for a in acts] + ([d.start()] if d else [])
    return min(cands) if cands else len(f)


_LOC_BEFORE_RE = re.compile(
    r"(?:αρθρ\w*|παρ\.|παραγραφ\w*|περ\.|περιπτωσ\w*|εδαφι\w*|τιτλ\w*|παραρτημ\w*|στοιχει\w*)"
    r"\s?[\w’).]*\s+(?:του|τησ|των)"
    # decisions: "της υπό στοιχεία ΥΠΕΝ/ΔΑΟΚΑ/…", "της υπ’ αριθμ. …", "της … κοινής υπουργικής απόφασης"
    r"(?:\s+(?:υπ’?\s?αριθμ\.?|υπο\s+στοιχεια|αριθμ\.?|(?:κοινησ\s+)?υπουργικησ\s+αποφασησ|αποφασησ))*"
    r"\s*[\w.]*/?[\w/.]*?\s*$"
)
_SUBJECT_BEFORE_RE = re.compile(r"(?:^|[\s,.:])(?:ο|στον|στο|τον)\s*$")
# "από τις 3.11.2017, ημερομηνία δημοσίευσης του ν. 4495/2017": a date, never the target
_DATE_REF_RE = re.compile(r"ημερομηνι\w*\s+(?:δημοσιευσ\w*|εναρξ\w*(?:\s+τησ)?\s+ισχυ\w*|ισχυ\w*|κυκλοφορι\w*)\s+(?:του|τησ)\s*$")


def _strip_parens(t: str) -> str:
    return re.sub(r"\([^()]*\)", "", t)


def _historical(r: ActRef, f: str, upto: int) -> bool:
    """True if the citation sits inside an "όπως τροποποιήθηκε με ..." history clause,
    or is only used as a date ("ημερομηνία δημοσίευσης του ν. ...")."""
    if _DATE_REF_RE.search(f[max(0, r.start - 60): r.start]):
        return True
    for h in HISTORY_CLAUSE_RE.finditer(f, 0, upto):
        if h.start() < r.start and "," not in _strip_parens(f[h.end(): r.start]):
            return True
    return False


def _pick_target(refs: list[ActRef], f: str, verb_pos: int) -> ActRef | None:
    pre = [r for r in refs if r.start < verb_pos and not _historical(r, f, verb_pos)]
    for r in pre:
        window = f[max(0, r.start - 80): r.start]
        if _LOC_BEFORE_RE.search(window) or _SUBJECT_BEFORE_RE.search(window[-12:]):
            return r
    # weak fallback (no locator right before the citation): accept only if the verb follows closely,
    # e.g. "... του Κώδικα (ν. 5222/2025, Α’ 134), περί ..., επέρχονται"
    for r in pre:
        if 0 <= verb_pos - r.end <= 160 and "(" in f[max(0, r.start - 3): r.start + 1]:
            return r
        if 0 <= verb_pos - r.end <= 60:
            return r
    if pre:
        return None
    # verb-first: "Καταργείται η παρ. 3 του άρθρου 5 του ν. 4495/2017", "Αναστέλλεται ... του ν. X"
    stop = len(f)
    for marker in ("ωσ εξησ", "⟦"):
        i = f.find(marker, verb_pos)
        if i >= 0:
            stop = min(stop, i)
    post = [r for r in refs if verb_pos <= r.start < stop and not _historical(r, f, r.start)]
    for r in post:
        window = f[max(0, r.start - 80): r.start]
        if _LOC_BEFORE_RE.search(window) or _SUBJECT_BEFORE_RE.search(window[-12:]):
            return r
    return None


_SENT_END_RE = re.compile(r"[.;]\s+(?=[Α-ΩΆΈΉΊΌΎΏ«])|\n")
_ABBR_BEFORE_DOT = {"παρ", "περ", "αρ", "αρθ", "εδ", "ν", "π", "δ", "σελ", "αριθ", "αριθμ", "υπ", "υποπερ", "υποπαρ",
                    "στοιχ", "κεφ", "τευχ", "φεκ", "κλπ", "λπ", "πχ", "δηλ", "βλ", "κ", "διορθ", "σφαλμ", "αποφ", "κα",
                    "υπουργ", "γεν", "δ/νση", "τμ", "εκ", "αρμ"}


def _sentence_span(text: str, pos: int) -> tuple[int, int]:
    """Bounds of the sentence of `text` (folded) that contains offset `pos` (abbreviation-aware)."""
    start = 0
    for m in _SENT_END_RE.finditer(text):
        if m.group(0) != "\n":
            tok = re.search(r"([\w.’/]+)$", text[max(0, m.start() - 15): m.start()])
            t = fold(tok.group(1)) if tok else ""
            if t.replace(".", "") in _ABBR_BEFORE_DOT or "." in t or (len(t) == 1 and t.isalpha()):
                continue
        if m.end() <= pos:
            start = m.end()
        elif m.start() >= pos:
            return start, m.start()
    return start, len(text)


def _verb_in_sentence(text: str, f: str, r: ActRef) -> bool:
    s, e = _sentence_span(text, r.start)  # boundaries need the original case
    seg = f[s:e]
    return bool(find_actions(seg)) or bool(re.search(r"επερχ(?:εται|ονται)", seg))


def _resolve(instr: str, ctx_target: str | None, ctx_how: str, aliases: dict[str, list[str]]):
    f = fold(instr)
    verb_pos = _first_verb_pos(f)
    refs = find_act_refs(instr, f)
    alias_refs = []
    for key, names in aliases.items():
        try:
            kind, num, yr = parse_act_key(key)
        except ValueError:
            continue
        for name in names:
            for m in re.finditer(re.escape(fold(name)), f):
                if not any(r.start <= m.start() < r.end + 25 for r in refs):  # "Κώδικα (ν. X)" already cited
                    alias_refs.append(ActRef(kind, num, yr, m.start(), m.end()))
    all_refs = sorted(refs + alias_refs, key=lambda r: r.start)
    target = _pick_target(all_refs, f, verb_pos)
    via_alias = target is not None and target in alias_refs
    if target is not None and target.end <= verb_pos and _SELF_SUBJECT_RE.search(f[target.end: verb_pos]):
        target = None  # "..., το παρόν άρθρο καταργείται": the amending act acts on itself
    if target is not None and not _verb_in_sentence(instr, f, target):
        target = None  # cited, but the amending verb belongs to another sentence
        verb_pos = len(f) if not refs else verb_pos
    how = ("alias" if via_alias else "explicit") if target else "none"
    tkey = target.key if target else None
    if tkey is None and _SAME_LAW_RE.search(f[:verb_pos]) and ctx_target:
        tkey, how = ctx_target, "same-law"
    loc_text = instr[: target.start] if target else instr[:verb_pos]
    if target is not None and target.start > verb_pos:
        loc_text = instr[verb_pos: target.start]  # verb-first order
    loc = parse_locator(loc_text)
    if loc.is_empty() and target is not None and target.end < verb_pos:
        # "Στον ν. 4495/2017, μετά από το άρθρο 25Α προστίθεται ..."
        loc = parse_locator(PH_RE.sub(" ", instr[target.end: verb_pos]))
    if tkey is None and ctx_target and not _SELF_REF_RE.search(f[:verb_pos]):
        if not loc.is_empty():
            tkey, how = ctx_target, ctx_how
    return tkey, how, loc, refs, f, verb_pos


def analyze(doc: ExtractedDoc, year: int | None = None, aliases: dict[str, list[str]] | None = None,
            issue_date: str | None = None) -> ActAnalysis:
    """Analyse one gazette issue. `aliases` maps act keys to names used instead of numbers."""
    aliases = aliases or {}
    payloads: dict[int, Payload] = {}
    next_id = [1]
    provisions: list[Provision] = []
    all_articles: list[ActArticle] = []
    acts = segment(doc, year, issue_date)
    main_act = next((a["key"] for a in acts if a["key"]), None)
    main_title = next((a["title"] for a in acts if a["key"]), acts[0]["title"] if acts else "")
    for act in acts:
        act_key = act["key"] or main_act or "?"
        for art in act["articles"]:
            all_articles.append(art)
            lines, pages = _split_quotes(art.paras, payloads, next_id)
            # context from the article title, e.g. "Τροποποιήσεις του ν. 4495/2017 (Α’ 167)"
            title_refs = find_act_refs(art.title)
            title_keys = {r.key for r in title_refs}
            ctx_target, ctx_how = (next(iter(title_keys)), "inherited-title") if len(title_keys) == 1 else (None, "none")
            lead_target = None   # "Ο ν. 4495/2017 τροποποιείται ως εξής:" at the start of the article
            prev_target = None   # last explicit target (weak fallback)
            for label, text, page in _split_provisions(lines, pages):
                if lead_target:
                    inh, inh_how = lead_target, "inherited-lead"
                elif ctx_target:
                    inh, inh_how = ctx_target, ctx_how
                else:
                    inh, inh_how = prev_target, "inherited-previous"
                tkey, how, loc, refs, f, verb_pos = _resolve(text, inh, inh_how, aliases)
                acts_found = sorted({a[0] for a in find_actions(f)})
                new_arts = _added_articles(f, verb_pos) if "add" in acts_found else []
                pids = [int(x) for x in PH_RE.findall(text)]
                cited = sorted({r.key for r in refs})
                qcited = set()
                for pid in pids:
                    for r in find_act_refs(payloads[pid].text):
                        qcited.add(r.key)
                qcited -= set(cited)
                if tkey and acts_found and (how not in ("explicit",) or any(r.key == tkey and _verb_in_sentence(text, f, r) for r in refs)):
                    kind = "amendment"
                elif DEROGATION_RE.search(f) and refs:
                    kind = "derogation"
                elif refs:
                    kind = "reference"
                elif qcited:
                    kind = "quoted"
                else:
                    kind = "other"
                ld = loc.to_dict()
                affected = list(new_arts) or ([ld["article"]] if ld.get("article") else [])
                prov = Provision(
                    act=act_key, article=art.label, article_title=art.title, label=label, page=page,
                    instruction=text, payload_ids=pids, target=tkey if kind == "amendment" else (tkey or None),
                    target_how=how, locator=ld, actions=acts_found, kind=kind,
                    cited=cited, quoted_cited=sorted(qcited), articles=affected,
                )
                provisions.append(prov)
                if kind == "amendment" and how in ("explicit", "alias"):
                    prev_target = tkey
                    # a lead-in: names the act, has no new text of its own, introduces a list
                    if not pids and loc.is_empty() and (label == "" or re.search(r"εξησ\s*:?\s*$|ακολουθ\w*\s*:?\s*$", f.strip())):
                        lead_target = tkey
    # A decision/decree titled "Τροποποίηση της … απόφασης «…»" amends that act as a whole, even when its body
    # republishes the text instead of listing "η παρ. Χ αντικαθίσταται" instructions.
    for act in acts:
        title = _TITLE_TAIL_RE.sub("", act["title"] or "").strip()
        tm = _TITLE_AMEND_RE.match(fold(title))
        if not tm:
            continue
        f = fold(title)
        targets = [r.key for r in find_act_refs(title, f)]
        for key, names in aliases.items():
            if any(fold(n) in f for n in names):
                targets.append(key)
        specific = {p.target for p in provisions if p.kind == "amendment" and p.act == (act["key"] or main_act or "?")}
        targets = [t for t in targets if t not in specific]  # already covered by specific provisions
        if not targets:
            continue
        page = act["articles"][0].page if act["articles"] else 1
        provisions.append(Provision(
            act=act["key"] or main_act or "?", article="", article_title=title, label="", page=page,
            instruction=title, payload_ids=[], target=targets[0], target_how="explicit", locator={},
            actions=[_TITLE_ACTIONS[tm.group(1)[:6]]], kind="amendment", cited=sorted(set(targets)),
            quoted_cited=[], whole_act=True))
    return ActAnalysis(main_act, main_title, [a["key"] for a in acts if a["key"]], all_articles, provisions, payloads)


# --------------------------------------------------------------------------- watching

@dataclass
class Hit:
    watched: str
    kind: str          # amendment | derogation | reference | quoted
    provision: Provision
    confidence: float


def hits_for(analysis: ActAnalysis, watched: dict[str, dict]) -> list[Hit]:
    """Match provisions against watched acts.

    `watched` maps act key -> {"kind","number","year","aliases":[...]}"""
    out: list[Hit] = []
    for prov in analysis.provisions:
        for key, w in watched.items():
            if prov.act == key:
                continue  # the watched act itself (e.g. when backfilling its own FEK)
            if prov.kind == "amendment" and prov.target == key:
                conf = 1.0 if prov.target_how in ("explicit", "alias") else 0.85
                out.append(Hit(key, "amendment", prov, conf))
                continue
            instr = prov.instruction
            ms = find_mentions(instr, w["kind"], w["number"], w["year"], w.get("aliases"))
            if ms:
                kind = "derogation" if prov.kind == "derogation" else "reference"
                out.append(Hit(key, kind, prov, max(c for _, _, c in ms)))
                continue
            for pid in prov.payload_ids:
                pt = analysis.payload_text(pid)
                ms = find_mentions(pt, w["kind"], w["number"], w["year"], w.get("aliases"))
                if ms:
                    out.append(Hit(key, "quoted", prov, max(c for _, _, c in ms)))
                    break
    return out
