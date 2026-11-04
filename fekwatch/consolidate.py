"""Consolidation: apply detected amendments to the original text of an act.

Design rules (this output is used for legal work, so it must be checkable):

* Every change is applied from a structured operation parsed from the amending provision.
  The operation, the amending FEK/page and the previous wording are kept on the unit, so
  the HTML lets a reader compare old text → instruction → new text and open the FEK page.
* Anything we cannot parse, locate or apply unambiguously is *not* guessed: it is listed
  under "needs manual review" with a link to the provision.
* Amendments are applied in order of publication. Deferred entry into force, retroactive
  effect and transitional provisions are not modelled (stated in the output).
"""
from __future__ import annotations

import copy
import html
import re
from dataclasses import dataclass, field
from datetime import date

from .amendments import segment
from .citations import ACTION_LABELS_EL, Locator, greek_letter_value, ordinal_value, parse_locator
from .config import Config, WatchedAct
from .et_api import EtClient, FekMeta
from .normalize import fold
from .pdftext import extract_pdf
from .store import Store, now_iso
from .glossary import TIP_CSS, legend_html, tip

# --------------------------------------------------------------------------- model


@dataclass
class Note:
    n: int
    record_id: str
    fek: str
    date: str
    act: str
    where: str          # "άρθρο 34 παρ. 3" of the amending act
    what: str           # human description of the operation
    pdf: str
    warning: str = ""


@dataclass
class Unit:
    kind: str                      # article | par | case | text
    label: str = ""
    title: str = ""
    text: str = ""
    children: list["Unit"] = field(default_factory=list)
    status: str = "active"         # active | repealed
    notes: list[Note] = field(default_factory=list)
    history: list[tuple[str, Note]] = field(default_factory=list)  # (previous wording, note)
    added: bool = False
    headings: list[str] = field(default_factory=list)  # ΜΕΡΟΣ/ΤΜΗΜΑ/ΚΕΦΑΛΑΙΟ printed before an article

    def full_text(self) -> str:
        parts = []
        if self.title:
            parts.append(self.title)
        if self.text:
            parts.append(self.text)
        for c in self.children:
            parts.append(c.render_plain())
        return "\n".join(parts)

    def render_plain(self) -> str:
        if self.status == "repealed":
            return f"{self.prefix()}[Καταργήθηκε]"
        lead = f"{self.prefix()}{self.text}".strip()
        kids = "\n".join(c.render_plain() for c in self.children)
        return "\n".join(x for x in (lead, kids) if x)

    def prefix(self) -> str:
        if self.kind == "par" and self.label:
            return f"{self.label}. "
        if self.kind == "case" and self.label:
            return f"{self.label}) "
        return ""


@dataclass
class Law:
    key: str
    title: str
    articles: list[Unit]
    preamble: list[str] = field(default_factory=list)
    status: str = "active"
    notes: list[Note] = field(default_factory=list)
    annexes: list[tuple[str, list[str]]] = field(default_factory=list)

    def article(self, label: str) -> Unit | None:
        lab = norm_art(label)
        return next((a for a in self.articles if norm_art(a.label) == lab), None)


def norm_art(label: str) -> str:
    s = fold(str(label)).replace(" ", "").strip("’.")
    w = ordinal_value(s) if s and not s[0].isdigit() else None
    return str(w) if w else s


def norm_lab(label: str) -> str:
    return fold(str(label)).replace(" ", "").strip("’).")


# --------------------------------------------------------------------------- parsing text into units

PAR_RE = re.compile(r"^(\d{1,3}[α-ωΑ-Ω]?)\.\s*(?=\D|$)")
PAR_CASE_RE = re.compile(r"^(\d{1,3}[α-ωΑ-Ω]?)\.\s?([α-ω]{1,3})\)\s*")
CASE_RE = re.compile(r"^([α-ω]{1,4}|[ivx]{1,5})(?:\)|’\)|’\.?|\.)\s+")
ART_LINE_RE = re.compile(r"^(?:Άρθρο|ΑΡΘΡΟ|ΆΡΘΡΟ)\s+(\d{1,4}[Α-Ωα-ω]{0,3}|[α-ωάέήίόύώ]+(?:\s[α-ωάέήίόύώ]+)?)\s*$")
_TERMINAL = tuple(".;:»)!·")


def _case_level(label: str, stack_labels: list[str]) -> int:
    s = norm_lab(label)
    if re.fullmatch(r"[ivx]+", s):
        return 3
    if len(s) == 1 or s == "στ":
        return 1
    if len(s) >= 2 and len(set(s)) == 1:
        return 2
    if s.startswith("στ") and len(s) == 4 and s[2:] == "στ":
        return 2
    # "αβ", "αγ" ... sub-items of "α"; but "ια", "ιβ", "κα" are cases 11, 12, 21
    parent = stack_labels[0] if stack_labels else None
    if parent and s[0] == norm_lab(parent)[0] and len(s) == 2 and parent != s:
        pv = greek_letter_value(parent) or 0
        sv = greek_letter_value(s)
        if sv is not None and sv == pv + 1:
            return 1
        return 2
    return 1 if greek_letter_value(s) is not None else 2


def parse_units(lines: list[str]) -> tuple[list[Unit], str]:
    """Parse the body of an article (list of paragraphs) into units.

    Returns (children, intro_text) where intro_text is unnumbered text before the first
    numbered paragraph (kept as a 'text' unit)."""
    root = Unit("article")
    cur_par: Unit | None = None
    case_stack: list[Unit] = []  # [level1, level2, level3]

    def container() -> Unit:
        return cur_par if cur_par is not None else root

    def attach_case(u: Unit, level: int):
        nonlocal case_stack
        while len(case_stack) >= level:
            case_stack.pop()
        parent = case_stack[-1] if case_stack else container()
        parent.children.append(u)
        case_stack.append(u)

    last: Unit | None = None
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        m = PAR_CASE_RE.match(line)
        if m and greek_letter_value(m.group(2)) is not None:
            cur_par = Unit("par", m.group(1))
            root.children.append(cur_par)
            case_stack = []
            c = Unit("case", m.group(2), text=line[m.end():].strip())
            attach_case(c, 1)
            last = c
            continue
        m = PAR_RE.match(line)
        if m:
            cur_par = Unit("par", m.group(1), text=line[m.end():].strip())
            root.children.append(cur_par)
            case_stack = []
            last = cur_par
            continue
        m = CASE_RE.match(line)
        if m and (greek_letter_value(m.group(1)) is not None or re.fullmatch(r"[ivx]+", m.group(1))):
            level = _case_level(m.group(1), [c.label for c in case_stack])
            level = min(level, len(case_stack) + 1)
            c = Unit("case", m.group(1), text=line[m.end():].strip())
            attach_case(c, level)
            last = c
            continue
        # unlabeled paragraph
        if last is not None and last.text and not last.text.endswith(_TERMINAL) and line[:1].islower():
            last.text = f"{last.text} {line}"  # visual line break inside a sentence
            continue
        if case_stack:
            # text after a list of cases belongs to the enclosing paragraph
            u = Unit("text", text=line)
            container().children.append(u)
            case_stack = []
            last = u
            continue
        target = cur_par if cur_par is not None else root
        if target.text and not target.children:
            target.text = f"{target.text}\n{line}"
            last = target
        else:
            u = Unit("text", text=line)
            target.children.append(u)
            last = u
    return root.children, root.text


def parse_payload(text: str) -> tuple[str | None, str, list[Unit], str]:
    """Parse replacement text. Returns (article_label|None, article_title, units, intro)."""
    lines = [l for l in text.split("\n") if l.strip()]
    art_label, art_title = None, ""
    if lines:
        m = ART_LINE_RE.match(lines[0].strip())
        m2 = re.match(r"^(?:Άρθρο|ΑΡΘΡΟ|ΆΡΘΡΟ)\s+(\d{1,4}[Α-Ωα-ω]{0,3})\s+(.+)$", lines[0].strip())
        if m:
            art_label = m.group(1)
            lines = lines[1:]
        elif m2 and not PAR_RE.match(m2.group(2)):
            art_label, art_title = m2.group(1), m2.group(2)
            lines = lines[1:]
        if art_label is not None:
            # title lines: until a numbered paragraph or a line that reads like body text
            while lines and not PAR_RE.match(lines[0]) and not CASE_RE.match(lines[0]) \
                    and not lines[0].rstrip().endswith((".", ":", ";")) and len(lines[0]) < 200:
                art_title = (art_title + " " + lines[0].strip()).strip()
                lines = lines[1:]
    units, intro = parse_units(lines)
    return art_label, art_title, units, intro


def build_law(key: str, doc, year: int) -> Law:
    acts = segment(doc, year)
    act = next((a for a in acts if a["key"] == key), None)
    if act is None and key.startswith("υ.α."):
        from .citations import decision_core, parse_act_key
        core = decision_core(parse_act_key(key)[1])
        act = next((a for a in acts if a["key"] and a["key"].startswith("απόφ.")
                    and decision_core(a["key"]) .startswith(core)), None)
    if act is None:
        act = acts[0] if acts else None
    if act is None:
        return Law(key, "", [])
    arts = []
    preamble = []
    annexes = []
    for a in act["articles"]:
        lines = [p.text for p in a.paras]
        if a.annex:
            annexes.append((a.title, lines))
            continue
        if not a.label:
            preamble += lines
            continue
        kids, intro = parse_units(lines)
        u = Unit("article", a.label, a.title, intro, kids, headings=list(a.headings))
        arts.append(u)
    return Law(key, act["title"], arts, preamble, annexes=annexes)


# --------------------------------------------------------------------------- sentences

_ABBR = {
    "ν", "παρ", "αρ", "αρθ", "περ", "εδ", "υποπερ", "υποπαρ", "κλπ", "λπ", "πχ", "δηλ", "βλ", "σελ", "αριθ", "αριθμ",
    "υπ", "τμ", "χλμ", "εκ", "κ", "στρ", "χιλ", "φεκ", "στοιχ", "κεφ", "τευχ", "αποφ", "σημ", "συν", "τηλ", "οδ", "αρχ",
    "μ", "τ", "πδ", "νδ", "κυα", "υα", "εδαφ", "αναλ", "κατ", "λ", "π", "δ", "α", "β", "γ", "ε", "ζ", "η", "θ", "ι",
}


def split_sentences(text: str) -> list[str]:
    """Split into εδάφια (sentences ending with a full stop), abbreviation-aware."""
    out, start = [], 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == ".":
            j = i + 1
            while j < n and text[j] in "»”)’":
                j += 1
            k = j
            while k < n and text[k] == " ":
                k += 1
            if k >= n:
                break
            nxt = text[k]
            if k > j or j > i + 1:
                if nxt.isupper() or nxt in "«“" or nxt.isdigit() and False:
                    w = re.search(r"([\w.]+)$", text[start:i])
                    tok = fold(w.group(1)) if w else ""
                    if not (tok.replace(".", "") in _ABBR or "." in tok or (len(tok) == 1 and tok.isalpha())):
                        out.append(text[start:j].strip())
                        start = k
                        i = k
                        continue
        i += 1
    out.append(text[start:].strip())
    return [s for s in out if s]


# --------------------------------------------------------------------------- operations

@dataclass
class Op:
    kind: str                 # replace_unit | add_unit | repeal_unit | replace_text | insert_text | delete_text |
                              # replace_sentence | add_sentence | repeal_sentence | renumber | replace_title | correct
    loc: Locator
    payload: str | None = None
    old: str | None = None
    new_kind: str | None = None   # for add_unit: article | par | case | sentence
    new_label: str | None = None
    position: str | None = None   # end | start | after:<label> | before:<label> | after_sentence:<n>
    everywhere: bool = False
    desc: str = ""
    restates: bool = False        # the payload restates the whole unit (supersedes other ops on it)


@dataclass
class Failure:
    record: dict
    reason: str
    text: str


_SUBITEM_SPLIT = re.compile(r"(?:(?<=^)|(?<=\n)|(?<=:\s)|(?<=,\s)|(?<=;\s)|(?<=\.\s)|(?<=⟧\s))([α-ω]{1,3})\)\s")
_PH = re.compile(r"⟦(\d+)⟧")
_WORDS = r"(?:(?:η|οι|τη|την|τισ|το|τα|ο|τον)\s)?(?:φρασ\w*|λεξ\w*|αριθμ\w*|ενδειξ\w*|εκφρασ\w*|ημερομηνι\w*|ποσ\w*|προθεσμι\w*|συντομογραφι\w*|αναφορ\w*|στοιχει\w*|τιμ\w*|χρονολογι\w*)?\s?"
RX_REPLACE_TEXT = re.compile(r"(?:οπου\s+(?:αναφερεται|αναφερονται|απανταται|απαντωνται|γινεται λογοσ για)\s+)?" + _WORDS +
                             r"⟦(\d+)⟧[^⟦⟧]{0,80}?αντικαθιστα(?:ται|νται)\s+(?:απο|με)\s+" + _WORDS + r"⟦(\d+)⟧")
RX_REPLACE_TEXT2 = re.compile(_WORDS + r"⟦(\d+)⟧[^⟦⟧]{0,40}?αντικαθιστα(?:ται|νται)\s+ωσ\s+εξησ\s*:?\s*⟦(\d+)⟧")
RX_INSERT_AFTER = re.compile(r"μετα\s+(?:απο\s+)?" + _WORDS + r"⟦(\d+)⟧[^⟦⟧]{0,80}?προστιθ(?:εται|ενται)\s+" + _WORDS + r"⟦(\d+)⟧")
RX_INSERT_BEFORE = re.compile(r"(?:πριν\s+(?:απο\s+)?|πριν\s+)" + _WORDS + r"⟦(\d+)⟧[^⟦⟧]{0,80}?προστιθ(?:εται|ενται)\s+" + _WORDS + r"⟦(\d+)⟧")
RX_INSERT_END = re.compile(r"στο\s+τελοσ[^⟦⟧]{0,120}?προστιθ(?:εται|ενται)\s+" + _WORDS + r"⟦(\d+)⟧")
RX_INSERT_START = re.compile(r"στην\s+αρχη[^⟦⟧]{0,120}?προστιθ(?:εται|ενται)\s+" + _WORDS + r"⟦(\d+)⟧")
RX_DELETE_TEXT = re.compile(_WORDS + r"⟦(\d+)⟧[^⟦⟧]{0,60}?(?:διαγραφ(?:εται|ονται)|απαλειφ(?:εται|ονται)|καταργ(?:ειται|ουνται))")
# verb-first variants: "αντικαθίσταται η φράση «Χ» με τη φράση «Υ»", "διαγράφεται η λέξη «Χ»",
# "προστίθεται η φράση «Υ» μετά τη λέξη «Χ»"
RX_REPLACE_TEXT_VF = re.compile(r"αντικαθιστα(?:ται|νται)\s+" + _WORDS + r"⟦(\d+)⟧[^⟦⟧]{0,40}?(?:απο|με)\s+" + _WORDS + r"⟦(\d+)⟧")
RX_DELETE_TEXT_VF = re.compile(r"(?:διαγραφ|απαλειφ)(?:εται|ονται)\s+" + _WORDS + r"⟦(\d+)⟧")
RX_INSERT_AFTER_VF = re.compile(r"προστιθ(?:εται|ενται)\s+" + _WORDS + r"⟦(\d+)⟧[^⟦⟧]{0,60}?μετα\s+(?:απο\s+)?" + _WORDS + r"⟦(\d+)⟧")
RX_CORRECT = re.compile(r"διορθων(?:εται|ονται)[^⟦⟧]{0,120}?⟦(\d+)⟧[^⟦⟧]{0,80}?(?:στο|στην|στον|στα|στισ)?\s*(?:ορθ\w*|σωστ\w*)?\s*⟦(\d+)⟧")
RX_UNIT_VERB = re.compile(r"(αντικαθιστα(?:ται|νται)|τροποποι(?:ειται|ουνται)|διαμορφων(?:εται|ονται)|προστιθ(?:εται|ενται)|"
                          r"καταργ(?:ειται|ουνται)|αναριθμ(?:ειται|ουνται))")
RX_ADD_WHAT = re.compile(r"προστιθ(?:εται|ενται)\s+(?:νε(?:α|ο|εσ|οι)\s+)?"
                         r"(παρ\.|παραγραφ\w*|περ\.|περιπτωσ\w*|υποπερ\.|υποπεριπτωσ\w*|εδαφι\w*|αρθρ\w*|στοιχει\w*"
                         r"|κεφαλαι\w*|τμημα\w*|μερο\w*|ενοτητ\w*|υποκεφαλαι\w*)"
                         r"\s?(\d{1,4}[α-ω]{0,3}(?![α-ω])|[α-ω]{1,3}’?\)?)?")
RX_RENUMBER = re.compile(r"αναριθμ(?:ειται|ουνται)\s+(?:σε|ωσ)\s+(?:παρ\.|παραγραφο|περ\.|περιπτωση|αρθρο)?\s*(\d{1,4}[α-ω]?|[α-ω]{1,3})")


def _strip_label(instr: str) -> str:
    return re.sub(r"^\s*\d{1,3}[α-ω]?\.\s*", "", instr)


def _split_subitems(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Split 'lead: α) ... β) ...' into lead and [(label, text)] at the top level."""
    f = fold(text)
    cuts = []
    expected = None
    for m in _SUBITEM_SPLIT.finditer(f):
        lab = m.group(1)
        v = greek_letter_value(lab)
        if v is None:
            continue
        if len(lab) == 1 or lab == "στ" or (len(lab) == 2 and lab[0] in "ικλ" and v and v > 10):
            if expected is None and v != 1:
                continue
            if expected is not None and v not in (expected, expected + 1):
                continue
            cuts.append((m.start(), m.end(), lab))
            expected = v + 1
    if not cuts:
        return text, []
    lead = text[: cuts[0][0]].strip()
    items = []
    for i, (s, e, lab) in enumerate(cuts):
        end = cuts[i + 1][0] if i + 1 < len(cuts) else len(text)
        items.append((lab, text[e:end].strip().rstrip(",;")))
    return lead, items


def _split_nested(text: str, parent_label: str) -> tuple[str, list[tuple[str, str]]]:
    """Split 'στο τρίτο εδάφιο, αα) ..., αβ) ...' (labels prefixed by the parent letter)."""
    f = fold(text)
    p = norm_lab(parent_label)
    rx = re.compile(r"(?:(?<=^)|(?<=\s))(" + re.escape(p) + r"[α-ω]|[α-ω]{2})\)\s")
    cuts = [(m.start(), m.end(), m.group(1)) for m in rx.finditer(f)]
    if not cuts:
        return text, []
    lead = text[: cuts[0][0]].strip()
    items = []
    for i, (s, e, lab) in enumerate(cuts):
        end = cuts[i + 1][0] if i + 1 < len(cuts) else len(text)
        items.append((lab, text[e:end].strip().rstrip(",;")))
    return lead, items


def _locator_of(text: str) -> Locator:
    """Locator of a clause: normally before the verb ("Η περ. β’ ... αντικαθίσταται"),
    but Greek also allows verb-first order ("Καταργείται η περ. θ’")."""
    f = fold(text)
    m = RX_UNIT_VERB.search(f)
    head = text[: m.start()] if m else text
    loc = parse_locator(_PH.sub(" ", head))
    if loc.is_empty() and m and not m.group(1).startswith("προστιθ"):
        tail = text[m.end():]
        cut = re.search(r"ως εξής|⟦", tail)
        loc = parse_locator(_PH.sub(" ", tail[: cut.start()] if cut else tail))
    return loc


_CLAUSE_SPLIT = re.compile(r"(?<=⟧)\s*\.\s+(?=[Α-ΩΆΈΉΊΌΎΏ])|(?<=[^\s.]{3})\.\s+(?=(?:Στ|Η |Οι |Το |Τα |Ο |Μετά|Καταργ|Αντικαθ|Προστ)[^⟦]{0,200}?"
                           r"(?:αντικαθίστα|προστίθ|καταργ|τροποποι|διαγράφ|αναριθμ))")


def _split_clauses(clause: str) -> list[str]:
    parts = [p.strip() for p in _CLAUSE_SPLIT.split(clause) if p and p.strip()]
    return parts or [clause]


def _merge(child: Locator, parent: Locator | None) -> Locator:
    return child.merged_into(parent) if parent else child


_RESTATE_RE = re.compile(
    r"(?:^|[\s,:;])(?:το|η|οι|ο|τα)\s+(?P<unit>[^⟦:;]{0,140}?)\s+"
    r"(?P<verb>διαμορφων(?:εται|ονται)|αντικαθιστα(?:ται|νται)|τροποποι(?:ειται|ουνται))"
    r"(?:\s+και\s+διαμορφων(?:εται|ονται))?\s+ωσ\s+(?:εξησ|ακολουθωσ)\s*:?\s*⟦(?P<pid>\d+)⟧\s*[.»”\s]*$")


def _restatement(text: str, payloads: dict[int, str], base: Locator) -> Op | None:
    """'... επέρχονται οι εξής τροποποιήσεις: α) ... β) ... και το άρθρο 199 διαμορφώνεται ως εξής: «...»'.

    When a provision ends by giving the full new wording of a unit, that wording is authoritative:
    apply it as one replacement and treat the listed changes as a summary."""
    f = fold(text)
    m = _RESTATE_RE.search(f)
    if not m:
        return None
    payload = payloads.get(int(m.group("pid")), "")
    restates_article = bool(re.match(r"^\s*(?:Άρθρο|ΑΡΘΡΟ|ΆΡΘΡΟ)\s+\S+", payload))
    has_list = bool(_split_subitems(text[: m.start()])[1]) or "επερχ" in f[: m.start()]
    if not (m.group("verb").startswith("διαμορφ") or (restates_article and has_list)):
        return None
    unit_text = text[m.start("unit"): m.end("unit")]
    inner = parse_locator(_PH.sub(" ", unit_text))
    if inner.is_empty():
        if not (re.search(r"\bαρθρ", m.group("unit")) or restates_article):
            return None
        loc = Locator(article=base.article)
    else:
        loc = _merge(inner, base)
    if not loc.article:
        return None
    if restates_article and (loc.paragraph or loc.case):
        loc = Locator(article=loc.article)  # the payload is the whole article
    summary = _short(_PH.sub("«…»", text[: m.start()]).strip(), 160)
    return Op("replace_unit", loc, payload=payload, restates=True,
              desc="Νέα διατύπωση (όπως την παραθέτει το ΦΕΚ)" + (f" — περιλαμβάνει: {summary}" if summary else ""))


def parse_ops(instruction: str, payloads: dict[int, str], base: Locator) -> tuple[list[Op], list[str]]:
    """Parse one amending provision into operations. Returns (ops, problems)."""
    text = _strip_label(instruction).strip()
    whole = _restatement(text, payloads, base)
    if whole is not None:
        return [whole], []
    lead, items = _split_subitems(text)
    ops: list[Op] = []
    problems: list[str] = []
    lead_loc = _merge(_locator_of(lead), base) if lead else base
    if items:
        lead_ops, _ = _clause_ops(lead, payloads, lead_loc) if _PH.search(lead) else ([], [])
        ops += lead_ops
        for lab, itxt in items:
            sub_lead, nested = _split_nested(itxt, lab)
            if nested:
                sl = _merge(_locator_of(sub_lead), lead_loc)
                for _, ntxt in nested:
                    o, p = _clause_ops(ntxt, payloads, sl, subitem=True)
                    ops += o
                    problems += p
            else:
                o, p = _clause_ops(itxt, payloads, lead_loc, subitem=True)
                ops += o
                problems += p
    else:
        o, p = _clause_ops(text, payloads, base, top=True)
        ops += o
        problems += p
    rest = [o for o in ops if o.restates]
    if rest:
        def inside(o, r):
            if o is r or o.restates:
                return False
            if r.loc.article != o.loc.article:
                return False
            return not r.loc.paragraph or r.loc.paragraph == o.loc.paragraph
        ops = [o for o in ops if not any(inside(o, r) for r in rest)]
    return ops, problems


def _clause_ops(clause: str, payloads: dict[int, str], ctx: Locator, top: bool = False,
                subitem: bool = False) -> tuple[list[Op], list[str]]:
    parts = _split_clauses(clause)
    if len(parts) > 1:
        ops, probs = [], []
        for part in parts:
            o, p = _one_clause_ops(part, payloads, ctx, top, subitem)
            ops += o
            probs += p
        return ops, probs
    return _one_clause_ops(clause, payloads, ctx, top, subitem)


_DESTRUCTIVE = {"replace_unit", "repeal_unit", "replace_sentence", "repeal_sentence", "replace_title"}


def _one_clause_ops(clause: str, payloads: dict[int, str], ctx: Locator, top: bool, subitem: bool):
    ops, probs = _one_clause_ops_raw(clause, payloads, ctx, top)
    if subitem:
        own = _locator_of(clause)
        if own.is_empty() and not own.sentence and any(o.kind in _DESTRUCTIVE for o in ops):
            return [], [f"η υποπερίπτωση δεν ορίζει ποια διάταξη αφορά: «{_short(clause, 90)}»"]
    return ops, probs


def _one_clause_ops_raw(clause: str, payloads: dict[int, str], ctx: Locator, top: bool = False) -> tuple[list[Op], list[str]]:
    f = fold(clause)
    ops: list[Op] = []
    loc = _merge(_locator_of(clause), ctx)
    P = lambda i: payloads.get(int(i), "")  # noqa: E731
    everywhere = bool(re.search(r"\bοπου\b", f))
    # ---- text-level operations (may be several in one clause)
    used: set[str] = set()
    for m in RX_CORRECT.finditer(f):
        ops.append(Op("correct", loc, old=P(m.group(1)), payload=P(m.group(2)), desc="Διόρθωση σφάλματος"))
        used |= {m.group(1), m.group(2)}
    for m in RX_REPLACE_TEXT.finditer(f):
        if m.group(1) in used:
            continue
        ops.append(Op("replace_text", loc, old=P(m.group(1)), payload=P(m.group(2)), everywhere=everywhere,
                      desc=f"Αντικατάσταση λέξεων «{_short(P(m.group(1)))}»"))
        used |= {m.group(1), m.group(2)}
    for m in RX_REPLACE_TEXT2.finditer(f):
        if m.group(1) in used:
            continue
        ops.append(Op("replace_text", loc, old=P(m.group(1)), payload=P(m.group(2)),
                      desc=f"Αντικατάσταση φράσης «{_short(P(m.group(1)))}»"))
        used |= {m.group(1), m.group(2)}
    for m in RX_REPLACE_TEXT_VF.finditer(f):
        if m.group(1) in used:
            continue
        ops.append(Op("replace_text", loc, old=P(m.group(1)), payload=P(m.group(2)), everywhere=everywhere,
                      desc=f"Αντικατάσταση λέξεων «{_short(P(m.group(1)))}»"))
        used |= {m.group(1), m.group(2)}
    for m in RX_INSERT_AFTER_VF.finditer(f):
        if m.group(2) in used:
            continue
        ops.append(Op("insert_text", loc, old=P(m.group(2)), payload=P(m.group(1)), position="after", everywhere=everywhere,
                      desc=f"Προσθήκη λέξεων μετά από «{_short(P(m.group(2)))}»"))
        used |= {m.group(1), m.group(2)}
    for m in RX_DELETE_TEXT_VF.finditer(f):
        if m.group(1) in used:
            continue
        ops.append(Op("delete_text", loc, old=P(m.group(1)), everywhere=everywhere,
                      desc=f"Διαγραφή λέξεων «{_short(P(m.group(1)))}»"))
        used.add(m.group(1))
    for rx, pos in ((RX_INSERT_AFTER, "after"), (RX_INSERT_BEFORE, "before")):
        for m in rx.finditer(f):
            if m.group(1) in used:
                continue
            ops.append(Op("insert_text", loc, old=P(m.group(1)), payload=P(m.group(2)), position=pos, everywhere=everywhere,
                          desc=f"Προσθήκη λέξεων {'μετά' if pos == 'after' else 'πριν'} από «{_short(P(m.group(1)))}»"))
            used |= {m.group(1), m.group(2)}
    for m in RX_DELETE_TEXT.finditer(f):
        if m.group(1) in used:
            continue
        # "Η περ. β’ ... καταργείται" has no quoted words before the verb -> unit repeal, handled below
        ops.append(Op("delete_text", loc, old=P(m.group(1)), everywhere=everywhere,
                      desc=f"Διαγραφή λέξεων «{_short(P(m.group(1)))}»"))
        used.add(m.group(1))
    if ops:
        return ops, []
    ph = _PH.findall(clause)
    for rx, pos in ((RX_INSERT_END, "end"), (RX_INSERT_START, "start")):
        m = rx.search(f)
        if m and not RX_ADD_WHAT.search(f):
            return [Op("insert_text", loc, payload=P(m.group(1)), position=pos,
                       desc="Προσθήκη λέξεων στο " + ("τέλος" if pos == "end" else "αρχή"))], []
    vm = RX_UNIT_VERB.search(f)
    if not vm:
        return [], [] if not top else ["Δεν βρέθηκε ρήμα τροποποίησης"]
    verb = vm.group(1)
    after = [m for m in _PH.finditer(clause) if m.start() >= vm.start()]
    payload = P(after[0].group(1)) if after else (P(ph[-1]) if ph else None)
    # "... προστίθεται περ. κβ’ και το άρθρο 30 διαμορφώνεται ως εξής: «Άρθρο 30 ...»": the unit named
    # before "διαμορφώνεται" is restated in full; that restatement is what must be applied.
    dm = re.search(r"(?:και|,)\s+(?:η|το|ο|οι|τα)\s+([^⟦]{0,80}?)\s+διαμορφων(?:εται|ονται)\s+ωσ\s+εξησ\s*:?\s*⟦(\d+)⟧", f)
    if dm:
        inner = parse_locator(clause[dm.start(1): dm.end(1)])
        whole = _merge(inner, ctx) if not inner.is_empty() else Locator(article=loc.article)
        return [Op("replace_unit", whole, payload=P(dm.group(2)), desc="Τροποποίηση (νέα διατύπωση)", restates=True)], []
    if verb.startswith("καταργ"):
        if loc.is_empty():
            return [Op("repeal_unit", loc, desc="Κατάργηση ολόκληρης της πράξης")], []
        return [Op("repeal_sentence" if loc.sentence else "repeal_unit", loc, desc="Κατάργηση")], []
    if verb.startswith("αναριθμ"):
        m = RX_RENUMBER.search(f)
        if m:
            return [Op("renumber", loc, new_label=m.group(1), desc=f"Αναρίθμηση σε {m.group(1)}")], []
        return [], ["Αναρίθμηση που δεν αναγνωρίστηκε"]
    if verb.startswith("προστιθ"):
        if payload is None:
            return [], ["Προσθήκη χωρίς παρατιθέμενο κείμενο"]
        m = RX_ADD_WHAT.search(f)
        what = fold(m.group(1)) if m else ""
        new_label = norm_label_str(m.group(2)) if m and m.group(2) else None
        if new_label and not new_label[0].isdigit() and greek_letter_value(new_label) is None:
            new_label = None  # e.g. "προστίθεται εδάφιο ως εξής"
        if what.startswith(("κεφαλαι", "τμημα", "μερο", "ενοτητ", "υποκεφαλαι")):
            return [Op("add_heading", loc, payload=payload, desc="Προσθήκη επικεφαλίδας ενότητας")], []
        nk = ("article" if what.startswith("αρθρ") else "par" if what.startswith("παρ") else
              "case" if what.startswith(("περ", "υποπερ", "στοιχ")) else "sentence" if what.startswith("εδαφ") else None)
        head = f[: vm.start()]
        position = "end"
        pm = re.search(r"μετα\s+(?:απο\s+)?(?:την|το|τη)\s+(?:(\w+)\s+εδαφιο|παρ\.|παραγραφο|περ\.|περιπτωση|αρθρο)\s?(\d{1,4}[α-ω]?|[α-ω]{1,3})?", head)
        if pm:
            if pm.group(1) and ordinal_value(pm.group(1)):
                position = f"after_sentence:{ordinal_value(pm.group(1))}"
            elif pm.group(2):
                position = f"after:{norm_label_str(pm.group(2))}"
            # the locator parsed from "μετά την παρ. 2 του άρθρου 5" points at the anchor; the
            # container is one level up
            loc = _container_of(loc, nk)
        elif re.search(r"στην\s+αρχη", head):
            position = "start"
        elif nk in ("par", "case", "article"):
            loc = loc  # "Στο άρθρο 5 προστίθεται παρ. 6" : loc is the container
        if nk is None:
            nk = "sentence" if re.search(r"εδαφι", f) else None
        if nk is None:
            # e.g. "Στο τέλος της παρ. 2 προστίθεται: «...»" -> sentences at end
            nk = "sentence"
        return [Op("add_unit", loc, payload=payload, new_kind=nk, new_label=new_label, position=position,
                   desc=f"Προσθήκη {_KIND_EL.get(nk, nk)}{(' ' + new_label) if new_label else ''}")], []
    # replace / modify / διαμορφώνεται
    if payload is None:
        if verb.startswith("τροποποι"):
            return [], []  # container ("Το άρθρο 14 τροποποιείται ως εξής:" followed by sub-items)
        return [], ["Αντικατάσταση χωρίς παρατιθέμενο κείμενο"]
    if loc.title and not loc.paragraph:
        return [Op("replace_title", loc, payload=payload, desc="Αντικατάσταση τίτλου")], []
    if loc.sentence:
        return [Op("replace_sentence", loc, payload=payload, desc="Αντικατάσταση εδαφίου")], []
    if loc.is_empty():
        return [], ["Δεν προσδιορίστηκε η τροποποιούμενη διάταξη"]
    return [Op("replace_unit", loc, payload=payload,
               desc="Αντικατάσταση" if verb.startswith("αντικ") else "Τροποποίηση (νέα διατύπωση)")], []


_KIND_EL = {"article": "άρθρου", "par": "παραγράφου", "case": "περίπτωσης", "sentence": "εδαφίου"}


def norm_label_str(s: str) -> str:
    return fold(s).strip("’).").replace(" ", "")


def _container_of(loc: Locator, new_kind: str | None) -> Locator:
    out = copy.deepcopy(loc)
    if new_kind == "sentence":
        out.sentence = None
        return out
    if new_kind == "case":
        if out.subcase:
            out.subcase = None
        elif out.case:
            out.case = None
        return out
    if new_kind == "par":
        out.paragraph = None
        out.extra_paragraphs = []
        out.case = out.subcase = None
        return out
    if new_kind == "article":
        out.article = None
        return out
    return out


def _short(s: str, n: int = 40) -> str:
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "…"


# --------------------------------------------------------------------------- applying


class ApplyError(Exception):
    pass


def _find_child(parent: Unit, kind: str, label: str) -> Unit | None:
    lab = norm_lab(label)
    for c in parent.children:
        if c.kind == kind and norm_lab(c.label) == lab:
            return c
    return None


def _find_case_deep(parent: Unit, label: str) -> Unit | None:
    lab = norm_lab(label)
    for c in parent.children:
        if c.kind == "case" and norm_lab(c.label) == lab:
            return c
    for c in parent.children:
        if c.kind == "case":
            r = _find_case_deep(c, label)
            if r:
                return r
    return None


def locate(law: Law, loc: Locator) -> Unit:
    if not loc.article:
        raise ApplyError("δεν ορίζεται άρθρο")
    art = law.article(loc.article)
    if art is None:
        raise ApplyError(f"δεν βρέθηκε το άρθρο {loc.article}")
    u = art
    if loc.paragraph:
        p = _find_child(art, "par", loc.paragraph)
        if p is None and not loc.paragraph[0].isdigit():
            p = _find_child(art, "case", loc.paragraph)  # "παρ. γ’" used for a case
        if p is None and loc.paragraph == "1" and not any(c.kind == "par" for c in art.children):
            p = art  # single-paragraph article
        if p is None:
            raise ApplyError(f"δεν βρέθηκε η παρ. {loc.paragraph} του άρθρου {loc.article}")
        u = p
    for lv in ("case", "subcase", "element"):
        lab = getattr(loc, lv)
        if lab:
            c = _find_child(u, "case", lab) or (_find_case_deep(u, lab) if lv != "case" else None)
            if c is None and lv == "case":
                c = _find_case_deep(u, lab)
            if c is None:
                raise ApplyError(f"δεν βρέθηκε η {('περ.' if lv == 'case' else 'υποπερ.')} {lab} ({loc.describe()})")
            u = c
    return u


def _parent_of(root_units: list[Unit], target: Unit) -> tuple[Unit | None, int]:
    stack = [(None, root_units)]
    while stack:
        parent, kids = stack.pop()
        for i, c in enumerate(kids):
            if c is target:
                return parent, i
            stack.append((c, c.children))
    return None, -1


def _label_key(label: str):
    s = norm_lab(label)
    m = re.match(r"(\d+)([α-ω]*)", s)
    if m:
        return (int(m.group(1)), m.group(2))
    v = greek_letter_value(s)
    return (v if v is not None else 999, s)


def _insert_sorted(kids: list[Unit], new: Unit, position: str | None):
    if position and position.startswith("after:"):
        lab = norm_lab(position.split(":", 1)[1])
        for i, c in enumerate(kids):
            if norm_lab(c.label) == lab:
                kids.insert(i + 1, new)
                return
    if position == "start":
        kids.insert(0, new)
        return
    if new.label:
        key = _label_key(new.label)
        # only numbered siblings act as anchors (an "Άρθρο πρώτο" of a ratifying law must not)
        same = [i for i, c in enumerate(kids) if c.kind == new.kind and _label_key(c.label)[0] != 999]
        for i in same:
            if _label_key(kids[i].label) > key:
                kids.insert(i, new)
                return
        if same:
            # after the last unit of the same kind (keeps trailing text after it)
            kids.insert(same[-1] + 1, new)
            return
    kids.append(new)


def _merge_units(art: Unit, title: str, units: list[Unit], intro: str, note: Note) -> str:
    if title and _norm_ws(title) != _norm_ws(art.title):
        art.history.append((art.title, note))
        art.title = title
        art.notes.append(note)
    if intro and not units:
        raise ApplyError("το νέο κείμενο δεν περιέχει αριθμημένες παραγράφους για συγχώνευση")
    for nu in units:
        ex = _find_child(art, nu.kind, nu.label) if nu.label else None
        if ex is None:
            nu.added = True
            nu.notes.append(note)
            _insert_sorted(art.children, nu, None)
            continue
        ex.history.append((ex.render_plain(), note))
        ex.text, ex.children, ex.status = nu.text, nu.children, "active"
        ex.notes.append(note)
    return ""


def _payload_units(payload: str) -> tuple[str | None, str, list[Unit], str]:
    return parse_payload(payload)


def _set_content(target: Unit, payload: str, note: Note, keep_label=True):
    """Replace the content of `target` with the parsed payload."""
    art_label, art_title, units, intro = _payload_units(payload)
    if target.kind == "article" and art_label and norm_art(art_label) != norm_art(target.label):
        raise ApplyError(f"το νέο κείμενο είναι το «Άρθρο {art_label}», όχι το άρθρο {target.label}")
    if target.kind == "case" and units and not intro and units[0].kind == "par" and units[0].label:
        raise ApplyError(f"το νέο κείμενο είναι η παρ. {units[0].label}, όχι η περ. {target.label}")
    if target.kind != "article" and art_label:
        raise ApplyError(f"το νέο κείμενο είναι ολόκληρο το άρθρο {art_label}")
    if target.kind != "article" and len(units) >= 1 and not intro and units[0].label and units[0].kind == target.kind \
            and norm_lab(units[0].label) != norm_lab(target.label):
        raise ApplyError(f"το νέο κείμενο αρχίζει με «{units[0].prefix().strip()}» ενώ η διάταξη είναι "
                         f"{'η παρ.' if target.kind == 'par' else 'η περ.'} {target.label}")
    target.history.append((target.render_plain() if target.kind != "article" else target.full_text(), note))
    if target.kind == "article":
        if art_title:
            target.title = art_title
        target.text = intro
        target.children = units
    else:
        # payload usually starts with the unit's own label ("5. ..." / "β) ...")
        if len(units) == 1 and units[0].kind == target.kind:
            u = units[0]
            target.text = u.text
            target.children = u.children
        elif not units:
            target.text = intro
            target.children = []
        else:
            target.text = intro
            target.children = units
    target.status = "active"
    target.notes.append(note)


def _mark_changed(u: Unit, note: Note):
    u.notes.append(note)


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _find_text(u: Unit, needle: str) -> list[tuple[Unit, int, int]]:
    """Find `needle` in u and descendants; whitespace- and accent-insensitive."""
    n = _norm_ws(needle).strip()
    if not n:
        return []
    out = []
    fn = fold(n)
    for node in _walk(u):
        for attr in ("text", "title"):
            hay = getattr(node, attr)
            if not hay:
                continue
            hn = _norm_ws(hay)
            if hn != hay:
                setattr(node, attr, hn)
                hay = hn
            fh = fold(hay)
            start = 0
            while True:
                i = fh.find(fn, start)
                if i < 0:
                    break
                out.append((node, i, i + len(fn), attr))
                start = i + len(fn)
    return out


def _walk(u: Unit):
    yield u
    for c in u.children:
        yield from _walk(c)


def _edit_text(u: Unit, op: Op, note: Note) -> str:
    """Apply replace/insert/delete of words. Returns a warning ('' if clean)."""
    if op.kind == "insert_text" and op.position in ("end", "start"):
        node = u
        while node.children and node.kind != "text" and op.position == "end" and not node.text:
            node = node.children[-1]
        before = node.render_plain()
        t = node.text
        add = op.payload or ""
        if op.position == "end":
            if t.endswith(".") and not add.rstrip().endswith("."):
                node.text = t[:-1] + add.rstrip() + "."
            elif t.endswith(".") and add.rstrip().endswith("."):
                node.text = t[:-1] + add.rstrip() if add.startswith((",", ";", " ")) else t + " " + add.strip()
            else:
                node.text = (t + (" " if not add.startswith((",", ".", ";")) else "") + add).strip()
        else:
            node.text = (add.rstrip() + " " + t).strip()
        node.history.append((before, note))
        node.notes.append(note)
        return ""
    hits = _find_text(u, op.old or "")
    if not hits:
        raise ApplyError(f"δεν βρέθηκε η φράση «{_short(op.old or '', 80)}» στη διάταξη ({op.loc.describe()})")
    warning = ""
    if len(hits) > 1 and not op.everywhere:
        warning = f"η φράση εμφανίζεται {len(hits)} φορές· εφαρμόστηκε στην πρώτη"
        hits = hits[:1]
    seen = set()
    for node, i, j, attr in reversed(hits):
        hay = getattr(node, attr)
        if id(node) not in seen:
            node.history.append((node.render_plain(), note))
            node.notes.append(note)
            seen.add(id(node))
        new = op.payload or ""
        if op.kind == "replace_text" or op.kind == "correct":
            hay = hay[:i] + new + hay[j:]
        elif op.kind == "insert_text":
            if op.position == "before":
                sep = "" if new.endswith(" ") else " "
                hay = hay[:i] + new.rstrip() + sep + hay[i:]
            else:
                sep = "" if new[:1] in (",", ".", ";", ")", " ", "»") else " "
                hay = hay[:j] + sep + new.rstrip() + hay[j:]
        elif op.kind == "delete_text":
            hay = (hay[:i].rstrip() + " " + hay[j:].lstrip()).strip()
            hay = re.sub(r"\s+([,.;])", r"\1", hay)
        setattr(node, attr, _norm_ws(hay))
    return warning


def _sentence_slots(u: Unit) -> list[tuple[Unit, int]]:
    """Each εδάφιο of a unit as (node holding it, index of the sentence in node.text).

    A lead-in ending with ':' and its list of cases form one εδάφιο; text after the list
    (kept as 'text' children) continues the numbering."""
    slots = [(u, i) for i, _ in enumerate(split_sentences(u.text))] if u.text else []
    for c in u.children:
        if c.kind == "text" and c.status == "active":
            slots += [(c, i) for i, _ in enumerate(split_sentences(c.text))]
    return slots


def _sentence_edit(u: Unit, op: Op, note: Note):
    slots = _sentence_slots(u)
    k = op.loc.sentence
    if op.kind == "add_unit" and op.position and op.position.startswith("after_sentence:"):
        k = int(op.position.split(":")[1])
    n = len(slots)
    idx = (n + k) if k is not None and k < 0 else (k - 1 if k else None)
    new = _norm_ws(op.payload or "")
    if op.kind == "add_unit" and not (op.position and op.position.startswith("after_sentence:")):
        if op.position != "start" and u.kind == "article" and not u.text:
            pars = [c for c in u.children if c.kind == "par" and c.status == "active"]
            if pars:
                u = pars[-1]
                slots = _sentence_slots(u)
        if op.position == "start":
            node, sents = u, split_sentences(u.text)
            before = node.render_plain()
            node.text = " ".join([new] + sents)
        else:
            node = slots[-1][0] if slots else u
            if node is u and any(c.kind == "case" for c in u.children):
                node = Unit("text", text="")
                u.children.append(node)
            before = node.render_plain()
            node.text = " ".join(split_sentences(node.text) + [new])
        node.history.append((before, note))
        node.notes.append(note)
        return
    if idx is None or idx < 0 or idx >= n:
        raise ApplyError(f"δεν υπάρχει εδάφιο {k} στη διάταξη ({n} εδάφια βρέθηκαν)")
    node, i = slots[idx]
    sents = split_sentences(node.text)
    before = node.render_plain()
    if op.kind == "add_unit":
        sents.insert(i + 1, new)
    elif op.kind == "replace_sentence":
        sents[i] = new
    elif op.kind == "repeal_sentence":
        sents.pop(i)
    node.text = " ".join(sents)
    node.history.append((before, note))
    node.notes.append(note)


def apply_op(law: Law, op: Op, note: Note) -> str:
    """Apply one operation. Returns a warning string ('' if none). Raises ApplyError."""
    if op.kind == "add_heading":
        return "επικεφαλίδα ενότητας: δεν επηρεάζει το κείμενο των άρθρων"
    if op.kind == "repeal_unit" and op.loc.is_empty():
        law.status = "repealed"
        law.notes.append(note)
        return ""
    if op.kind == "correct" and not op.loc.article:
        # errata without a precise locator: search the whole act
        for art in law.articles:
            if _find_text(art, op.old or ""):
                return _edit_text(art, Op("replace_text", op.loc, old=op.old, payload=op.payload), note)
        raise ApplyError(f"δεν βρέθηκε το εσφαλμένο κείμενο «{_short(op.old or '', 80)}»")
    if op.kind == "add_unit" and op.new_kind == "article":
        art_label, art_title, units, intro = _payload_units(op.payload or "")
        label = op.new_label or art_label
        if not label:
            raise ApplyError("δεν προσδιορίστηκε ο αριθμός του νέου άρθρου")
        if law.article(label):
            existing = law.article(label)
            if existing.status == "repealed":
                _set_content(existing, op.payload or "", note)
                return ""
            raise ApplyError(f"το άρθρο {label} υπάρχει ήδη")
        new = Unit("article", label.upper() if label[0].isdigit() else label, art_title, intro, units, added=True)
        new.notes.append(note)
        _insert_sorted(law.articles, new, op.position if op.position != "end" else None)
        return ""
    if op.kind == "renumber":
        u = locate(law, op.loc)
        before = u.label
        u.label = op.new_label or u.label
        u.notes.append(note)
        u.history.append((f"(αρίθμηση {before})", note))
        return ""

    # everything else needs the target unit
    if op.kind == "add_unit" and op.new_kind in ("par", "case"):
        container = locate(law, op.loc) if (op.loc.paragraph or op.loc.case) else law.article(op.loc.article or "")
        if container is None:
            raise ApplyError(f"δεν βρέθηκε το άρθρο {op.loc.article}")
        _a_label, _t, units, intro = _payload_units(op.payload or "")
        if not units:
            units = [Unit(op.new_kind, op.new_label or "", text=intro)]
        elif intro:
            units[0].text = (intro + " " + units[0].text).strip()
        if op.new_kind == "par" and container.kind == "article" and not any(c.kind == "par" for c in container.children) \
                and (container.text or container.children):
            # first numbered paragraph is added to an unnumbered article: existing text becomes par. 1
            p1 = Unit("par", "1", text=container.text, children=container.children)
            container.text, container.children = "", [p1]
        warn = ""
        for nu in units:
            if op.new_label and len(units) == 1:
                nu.label = op.new_label if not nu.label else nu.label
            nu.added = True
            nu.notes.append(note)
            if nu.label and _find_child(container, nu.kind, nu.label):
                ex = _find_child(container, nu.kind, nu.label)
                if ex.status == "repealed":
                    _set_content(ex, op.payload or "", note)
                    continue
                warn = f"υπήρχε ήδη {nu.kind} {nu.label}· η νέα προστέθηκε μετά (πιθανή αναρίθμηση)"
                _insert_sorted(container.children, nu, f"after:{nu.label}")
                continue
            _insert_sorted(container.children, nu, op.position if op.position not in ("end",) else None)
        return warn
    if op.loc.extra_paragraphs and op.kind in ("replace_unit", "repeal_unit", "replace_text", "insert_text",
                                                "delete_text", "replace_sentence", "repeal_sentence"):
        # "Οι παρ. 2 και 3 ..." / "Η περ. β’ της παρ. 1 και της παρ. 3 ..."
        labels = [op.loc.paragraph] + op.loc.extra_paragraphs
        _a, _t, units, _i = _payload_units(op.payload or "") if op.kind == "replace_unit" else (None, "", [], "")
        by_label = {norm_lab(u.label): u for u in units if u.kind == "par" and u.label}
        distribute = op.kind == "replace_unit" and not op.loc.case and set(by_label) >= {norm_lab(l) for l in labels}
        warns = []
        for lab in labels:
            l2 = copy.deepcopy(op.loc)
            l2.paragraph, l2.extra_paragraphs = lab, []
            if distribute:
                ex = locate(law, l2)
                nu = by_label[norm_lab(lab)]
                ex.history.append((ex.render_plain(), note))
                ex.text, ex.children, ex.status = nu.text, nu.children, "active"
                ex.notes.append(note)
            else:
                w = apply_op(law, Op(op.kind, l2, op.payload, op.old, op.new_kind, op.new_label, op.position,
                                     op.everywhere, op.desc), note)
                if w:
                    warns.append(w)
        return "; ".join(warns)
    target = locate(law, op.loc)
    if op.kind in ("replace_sentence", "repeal_sentence") or (op.kind == "add_unit" and op.new_kind == "sentence"):
        _sentence_edit(target, op, note)
        return ""
    if op.kind in ("replace_text", "insert_text", "delete_text", "correct"):
        return _edit_text(target, op, note)
    if op.kind == "repeal_unit":
        target.history.append((target.render_plain() if target.kind != "article" else target.full_text(), note))
        target.status = "repealed"
        target.notes.append(note)
        return ""
    if op.kind == "replace_title":
        art = law.article(op.loc.article)
        art.history.append((art.title, note))
        art.title = _norm_ws(op.payload or "")
        art.notes.append(note)
        return ""
    if op.kind == "replace_unit":
        _a, _t, units, intro = _payload_units(op.payload or "")
        art = law.article(op.loc.article)
        if _a and art is not None and norm_art(_a) == norm_art(art.label) and target is not art:
            if op.loc.paragraph or op.loc.case:
                # "Τροποποιείται η παρ. 1 του άρθρου 89 ως εξής: «Άρθρο 89 / τίτλος / 1. ...»": the heading is
                # repeated for context only -> replace just the units that the new text contains.
                return _merge_units(art, _t, units, intro, note)
            target = art  # the new text restates the whole article
        elif units and not intro and units[0].kind == "par" and units[0].label and target.kind in ("case", "text") \
                and art is not None:
            par = _find_child(art, "par", units[0].label)
            if par is not None and any(u is target for u in _walk(par)):
                target = par  # the new text restates the whole paragraph that contains the case
        same = [u for u in units if u.kind == target.kind]
        if target.kind in ("par", "case") and not intro and len(units) > 1 and len(same) == len(units) \
                and units[0].label and norm_lab(units[0].label) == norm_lab(target.label):
            parent, _i = _parent_of(law.article(op.loc.article).children, target)
            parent = parent or law.article(op.loc.article)
            target.history.append((target.render_plain(), note))
            target.text, target.children, target.status = units[0].text, units[0].children, "active"
            target.notes.append(note)
            warns = []
            for nu in units[1:]:
                ex = _find_child(parent, nu.kind, nu.label) if nu.label else None
                if ex is not None:
                    ex.history.append((ex.render_plain(), note))
                    ex.text, ex.children, ex.status = nu.text, nu.children, "active"
                    ex.notes.append(note)
                    warns.append(f"αντικαταστάθηκε και η {nu.label}")
                else:
                    nu.added = True
                    nu.notes.append(note)
                    _insert_sorted(parent.children, nu, None)
            return "; ".join(warns)
        _set_content(target, op.payload or "", note)
        return ""
    raise ApplyError(f"μη υποστηριζόμενη ενέργεια {op.kind}")


# --------------------------------------------------------------------------- driver


@dataclass
class Result:
    law: Law
    as_of: str
    applied: list[tuple[dict, Op, str]] = field(default_factory=list)
    failed: list[Failure] = field(default_factory=list)
    skipped: list[tuple[dict, str]] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    source: dict = field(default_factory=dict)

    def stats(self) -> dict:
        return {"applied": len(self.applied), "failed": len(self.failed), "skipped": len(self.skipped),
                "provisions": len({r["id"] for r, *_ in self.applied} | {f.record["id"] for f in self.failed})}


def _where(r: dict) -> str:
    s = f"άρθρο {r['article']}" if r.get("article") else ""
    if r.get("par"):
        s += f" παρ. {r['par']}"
    return s.strip()


def consolidate_records(law: Law, records: list[dict], as_of: str | None = None) -> Result:
    recs = [r for r in records if r["kind"] == "amendment"]
    latest = max((r["source"].get("issue_date") or "" for r in recs
                  if not as_of or (r["source"].get("issue_date") or "") <= as_of), default="")
    res = Result(law, as_of or latest or date.today().isoformat())
    recs.sort(key=lambda r: (r["source"].get("issue_date") or "", r["source"]["number"],
                             _label_tuple(r.get("article", "")), _label_tuple(r.get("par", ""))))
    n = 0
    for r in recs:
        if as_of and (r["source"].get("issue_date") or "") > as_of:
            res.skipped.append((r, "μεταγενέστερο της ημερομηνίας ενοποίησης"))
            continue
        if not r.get("payloads") and not r.get("locator") and r.get("instruction", "").strip().endswith(":"):
            res.skipped.append((r, "εισαγωγική διάταξη (οι τροποποιήσεις ακολουθούν στις επόμενες παραγράφους)"))
            continue
        if r.get("whole_act"):
            res.failed.append(Failure(r, "Η πράξη τροποποιείται συνολικά (π.χ. αναδιάρθρωση ή νέο κείμενο) χωρίς "
                                         "επιμέρους οδηγίες «η παρ. Χ αντικαθίσταται»· διαβάστε το νέο κείμενο στο ΦΕΚ.",
                                      r.get("instruction", "")))
            continue
        textual = {"replace", "add", "repeal", "delete", "modify", "renumber", "correct"}
        if r.get("actions") and not (set(r["actions"]) & textual):
            res.skipped.append((r, "μη κειμενική μεταβολή (" + ", ".join(ACTION_LABELS_EL.get(a, a) for a in r["actions"]) + ")"))
            continue
        payloads = {p["id"]: p["text"] for p in r.get("payloads", [])}
        base = Locator(**r["locator"]) if r.get("locator") else Locator()
        try:
            ops, problems = parse_ops(r.get("instruction", ""), payloads, base)
        except Exception as exc:  # noqa: BLE001
            ops, problems = [], [f"σφάλμα ανάλυσης: {exc}"]
        if not ops or problems:
            # never apply a provision only in part: if any listed step could not be parsed, nothing
            # is applied and the whole provision goes to manual review
            reason = "; ".join(problems) or "η διάταξη δεν αναλύθηκε σε συγκεκριμένη ενέργεια"
            if ops and problems:
                reason = "δεν εφαρμόστηκε κανένα από τα βήματα, γιατί δεν αναγνωρίστηκαν όλα: " + reason
            res.failed.append(Failure(r, reason, r.get("instruction", "")))
            continue
        for op in ops:
            n += 1
            note = Note(n, r["id"], r["source"]["label"], r["source"].get("issue_date") or "", r["source"].get("act") or "",
                        _where(r), op.desc or op.kind, r["source"]["pdf"])
            try:
                warn = apply_op(law, op, note)
                note.warning = warn
                res.applied.append((r, op, warn))
                res.notes.append(note)
            except ApplyError as exc:
                res.failed.append(Failure(r, str(exc), op.desc))
            except Exception as exc:  # noqa: BLE001
                res.failed.append(Failure(r, f"εσωτερικό σφάλμα: {exc}", op.desc))
    return res


def _label_tuple(s: str):
    m = re.match(r"(\d+)", s or "")
    if m:
        return (int(m.group(1)), s)
    v = ordinal_value(s) if s else None
    return (v or 0, s or "")


def consolidate_law(cfg: Config, store: Store, client: EtClient, w: WatchedAct, as_of: str | None = None) -> Result:
    from .pipeline import resolve_act_fek
    law_d = store.load_law(w.slug, w.key)
    fek = law_d.get("fek") or resolve_act_fek(w, client)
    if not fek:
        raise SystemExit(f"Δεν βρέθηκε το ΦΕΚ δημοσίευσης της πράξης {w.key}")
    meta = FekMeta(fek["et_id"], fek["group"], fek["number"], fek["year"], fek.get("issue_date"),
                   fek.get("release_date"), fek.get("pages") or 0)
    doc = store.get_extracted(meta.et_id)
    if doc is None:
        doc = extract_pdf(client.download(meta, store.cache))
        store.put_extracted(meta.et_id, doc)
    law = build_law(w.key, doc, meta.year)
    res = consolidate_records(law, list(law_d["records"].values()), as_of)
    res.source = fek
    return res


# --------------------------------------------------------------------------- HTML

def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


CONS_CSS = """
.sec{text-align:center;font-weight:700;margin:26px 0 6px;letter-spacing:.02em}.sec.l0{font-size:19px;margin-top:40px}
.sec.l1{font-size:17px}.sec.l2{font-size:15px}.sec.l3,.sec.l4{font-size:14px;color:var(--muted)}
.toc .toch{margin:6px 0 2px}.toc .toch.l0{font-weight:700;margin-top:12px}.toc .toch.l1{padding-left:12px;font-weight:600}
.toc .toch.l2{padding-left:24px}.toc .toch.l3,.toc .toch.l4{padding-left:36px}
.toc .toca{display:inline-block;min-width:34px;margin:2px 2px 0 0;font-size:13px}
.art{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px 24px;margin:16px 0}
.art.changed{box-shadow:inset 3px 0 0 var(--accent)}.art.repealed{opacity:.6}
.arth{font-weight:600;text-align:center}.artt{font-weight:600;text-align:center;margin-bottom:8px}
.u{margin:4px 0;overflow-wrap:anywhere}.u.case{margin-left:22px}.u.case .u.case{margin-left:22px}.u.text{margin-left:0}
.u.changed>.ut{background:var(--changed-bg);border-radius:4px;padding:0 2px}.u.added>.ut{background:var(--added-bg);border-radius:4px;padding:0 2px}
.rep{color:var(--muted);font-style:italic}sup a{text-decoration:none;font-size:11px}
.notes{font-size:13px;color:var(--muted);border-top:1px solid var(--rule);margin-top:12px;padding-top:10px}
.notes li{margin:2px 0}.prev{font-size:13px;background:var(--raised);padding:10px 14px;margin:6px 0;white-space:pre-wrap;border-radius:8px;overflow-wrap:anywhere}
.fail{background:var(--warn-bg);padding:14px 18px;margin:10px 0;border-radius:10px}
.fail.info{background:var(--raised)}
details.prevd summary{cursor:pointer;font-size:12px;color:var(--muted)}
"""


def _note_refs(u: Unit) -> str:
    return "".join(f"<sup><a href=\"#n{n.n}\" title=\"{_e(n.fek)} — {_e(n.what)}\">[{n.n}]</a></sup>" for n in u.notes)


def _render_unit(u: Unit) -> str:
    cls = ["u", u.kind]
    if u.notes:
        cls.append("changed")
    if u.added:
        cls.append("added")
    prefix = _e(u.prefix())
    if u.status == "repealed":
        body = f"<span class=\"ut\">{prefix}<span class=\"rep\">[Καταργήθηκε]</span></span>{_note_refs(u)}"
    else:
        txt = _e(u.text).replace("\n", "<br>")
        body = f"<span class=\"ut\">{prefix}{txt}</span>{_note_refs(u)}"
    prev = ""
    if u.history:
        items = "".join(f"<div class=\"prev\"><b>Πριν από [{n.n}] {_e(n.fek)}:</b>\n{_e(t)}</div>" for t, n in u.history)
        prev = f"<details class=\"prevd\"><summary>προηγούμενες διατυπώσεις ({len(u.history)}){tip('previous')}</summary>{items}</details>"
    kids = "".join(_render_unit(c) for c in u.children) if u.status != "repealed" else ""
    return f"<div class=\"{' '.join(cls)}\">{body}{prev}{kids}</div>"


def _collect_notes(u: Unit, acc: list[Note]):
    acc += u.notes
    for c in u.children:
        _collect_notes(c, acc)


def render_html(w: WatchedAct, res: Result, updated: str | None = None) -> str:
    from .report import CSS, FONT_LINK, _fmt_date, bar
    from .glossary import TIP_CSS, legend_html, tip
    law = res.law
    st = res.stats()
    parts = [f"<h1>{_e(w.key)} — ενοποιημένο κείμενο</h1><p>{_e(law.title or w.name)}</p>"]
    src = res.source or {}
    parts.append(f"<p class=\"small\">Αρχικό κείμενο: <a href=\"{_e(src.get('pdf'))}\">ΦΕΚ {_e(src.get('label'))}</a> · "
                 f"ενοποίηση με τροποποιήσεις που δημοσιεύθηκαν έως {_e(_fmt_date(res.as_of))} · δεδομένα {_e((updated or now_iso())[:10])}</p>")
    parts.append(f"<div class=\"fail info\"><b>Πειραματική λειτουργία.</b>{tip('experimental')} "
                 "Το κείμενο προκύπτει από αυτόματη εφαρμογή των τροποποιήσεων κατά σειρά δημοσίευσης. Δεν λαμβάνονται υπόψη "
                 "μεταγενέστερη έναρξη ισχύος, αναδρομική ισχύς, μεταβατικές διατάξεις ή ακυρώσεις από δικαστήρια. "
                 "Κάθε αλλαγή φέρει σημείωση [n] με το ΦΕΚ και τη σελίδα, και κρατά την προηγούμενη διατύπωση για έλεγχο.</div>")
    parts.append(f"<div class=\"grid\"><div class=\"stat\"><b>{st['applied']}</b>αλλαγές εφαρμόστηκαν{tip('applied')}</div>"
                 f"<div class=\"stat\"><b class=\"{'warn' if st['failed'] else 'ok'}\">{st['failed']}</b>χρειάζονται χειροκίνητο έλεγχο{tip('review')}</div>"
                 f"<div class=\"stat\"><b>{len(law.articles)}</b>άρθρα</div></div>")
    parts.append(legend_html(["changed", "note", "previous", "applied", "review", "nontextual", "experimental"],
                             "Πώς διαβάζεται αυτή η σελίδα"))
    if law.status == "repealed":
        parts.append("<div class=\"fail\"><b>Η πράξη έχει καταργηθεί</b> (βλ. σημειώσεις).</div>")
    if res.failed:
        parts.append(f"<h2 id=\"review\">Χρειάζονται χειροκίνητο έλεγχο ({len(res.failed)}){tip('review')}</h2>"
                     "<p class=\"small\">Οι παρακάτω τροποποιήσεις εντοπίστηκαν, αλλά το εργαλείο <b>δεν</b> ήταν βέβαιο πώς να τις "
                     "περάσει στο κείμενο, οπότε <b>δεν</b> τις εφάρμοσε. Το κείμενο πιο κάτω τις παραλείπει μέχρι να τις ελέγξετε: "
                     "ανοίξτε το ΦΕΚ και δείτε τι αλλάζει. Ο «Λόγος» εξηγεί τι δεν ταίριαξε.</p>")
        for f in res.failed:
            r = f.record
            parts.append(f"<div class=\"fail\"><b><a href=\"{_e(r['source']['pdf'])}\">ΦΕΚ {_e(r['source']['label'])}</a></b> "
                         f"{_e(r['source'].get('act') or '')} {_e(_where(r))} → {_e(Locator(**r['locator']).describe() if r.get('locator') else '')}"
                         f"<br><span class=\"small\">Λόγος: {_e(f.reason)}</span>"
                         f"<details><summary class=\"small\">κείμενο διάταξης</summary><div class=\"prev\">{_e(_render_instr(r))}</div></details></div>")
    parts.append("<h2>Κείμενο</h2>")
    from .amendments import heading_level
    toc_items, hn = [], 0
    for ai, a in enumerate(law.articles):
        for h in a.headings:
            hn += 1
            toc_items.append(f"<div class=\"toch l{heading_level(h)}\"><a href=\"#h{hn}\">{_e(h)}</a></div>")
        toc_items.append(f"<a class=\"toca\" href=\"#art{ai}\">{_e(a.label)}</a>")
    if law.annexes:
        toc_items.append("<div class=\"toch l0\"><a href=\"#annexes\">Παραρτήματα</a></div>")
    parts.append(f"<details class=\"card toc\"><summary><b>Περιεχόμενα</b> ({len(law.articles)} άρθρα)</summary>"
                 f"{''.join(toc_items)}</details>")
    hn = 0
    for ai, a in enumerate(law.articles):
        for h in a.headings:
            hn += 1
            lv = heading_level(h)
            parts.append(f"<div class=\"sec l{lv}\" id=\"h{hn}\">{_e(h)}</div>")
        cls = "art" + (" changed" if a.notes or _any_notes(a) else "") + (" repealed" if a.status == "repealed" else "")
        notes: list[Note] = []
        _collect_notes(a, notes)
        notes = sorted({n.n: n for n in notes}.values(), key=lambda n: n.n)
        head = (f"<div class=\"arth\" id=\"art{ai}\">Άρθρο {_e(a.label)}{_note_refs(a)}</div>"
                f"<div class=\"artt\">{_e(a.title)}</div>")
        if a.status == "repealed":
            body = "<div class=\"rep\">[Καταργήθηκε]</div>"
        else:
            body = (f"<div class=\"u\">{_e(a.text)}</div>" if a.text else "") + "".join(_render_unit(c) for c in a.children)
        prev = ""
        if a.history:
            prev = ("<details class=\"prevd\"><summary>προηγούμενες διατυπώσεις του άρθρου</summary>" +
                    "".join(f"<div class=\"prev\"><b>Πριν από [{n.n}] {_e(n.fek)}:</b>\n{_e(t)}</div>" for t, n in a.history) +
                    "</details>")
        nl = ""
        if notes:
            nl = "<ol class=\"notes\" style=\"list-style:none;padding-left:0\">" + "".join(
                f"<li id=\"n{n.n}\">[{n.n}] {_e(n.what)} — {_e(n.where)} {_e(n.act)} "
                f"(<a href=\"{_e(n.pdf)}\">ΦΕΚ {_e(n.fek)}, {_e(_fmt_date(n.date))}</a>)"
                f"{(' <span class=warn>Προσοχή: ' + _e(n.warning) + '</span>') if n.warning else ''}</li>" for n in notes) + "</ol>"
        parts.append(f"<div class=\"{cls}\">{head}{body}{prev}{nl}</div>")
    if law.annexes:
        parts.append("<h2 id=\"annexes\">Παραρτήματα</h2><p class=\"small muted\">Τα παραρτήματα εμφανίζονται όπως "
                     "δημοσιεύθηκαν αρχικά· οι πίνακες μπορεί να μην αποδίδονται σωστά από το PDF.</p>")
        for title, lines in law.annexes:
            parts.append(f"<details class=\"card\"><summary><b>{_e(title)}</b></summary>"
                         f"<div class=\"prev\">{_e(chr(10).join(lines))}</div></details>")
    return (f"<!doctype html><html lang=\"el\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" "
            f"content=\"width=device-width,initial-scale=1\"><title>{_e(w.key)} — ενοποιημένο κείμενο</title>"
            f"{FONT_LINK}<style>{CSS}{CONS_CSS}{TIP_CSS}</style></head><body>"
            f"{bar('../', [('Τροποποιήσεις ' + w.key, w.slug + '.html')])}<main>{''.join(parts)}</main></body></html>")


def _any_notes(u: Unit) -> bool:
    return bool(u.notes) or any(_any_notes(c) for c in u.children)


def _render_instr(r: dict) -> str:
    payloads = {p["id"]: p["text"] for p in r.get("payloads", [])}
    return re.sub(r"⟦(\d+)⟧", lambda m: "«" + payloads.get(int(m.group(1)), "") + "»", r.get("instruction", ""))
