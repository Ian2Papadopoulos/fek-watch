# FEK Watch

**Παρακολούθηση ΦΕΚ για τροποποιήσεις νόμων.** Κάθε μέρα κατεβάζει τα νέα ΦΕΚ από το Εθνικό Τυπογραφείο,
εντοπίζει τις διατάξεις που τροποποιούν τους νόμους που σας ενδιαφέρουν και σας ειδοποιεί. Κρατά πλήρη λίστα με
όλα τα ΦΕΚ που έχουν τροποποιήσει κάθε νόμο και παράγει (πειραματικά) ενοποιημένο κείμενο. Κάθε αλλαγή συνδέεται με
τη σελίδα του ΦΕΚ, ώστε να ελέγχεται.

It does three things:

1. **Daily watch.** Every day it fetches the newly released gazette issues, finds provisions that amend
   (replace, add, repeal…) any law on your watchlist, and alerts you.
2. **Amendment history.** For any law it builds the full list of FEKs that amended it since publication:
   which article of which amending law changed which article/paragraph, with a link to the exact PDF page.
   It is cross-checked against the Printing Office's own (incomplete) links.
3. **Consolidated text (experimental).** It applies the amendments to the original text in publication
   order. Every changed paragraph carries a footnote to the amending FEK page and keeps the previous wording,
   so you can verify each change. Anything it cannot apply with certainty is listed for manual review and is
   never guessed.

## How accurate is it? (tested on ν. 4495/2017)

The new Spatial Planning Code (ν. 5306/2026, ΦΕΚ Α’ 88) codified 4495/2017. Its official correspondence table
lists, for every provision, the laws that amended it, and the Code re-enacts the consolidated wording. That gives
an independent answer key.

- **Which FEKs amend the law:** all **39 amending laws** in the official table were found (38 as textual
  amendments, 1 that creates a derogating regime as a *derogation*). The history scan checked 134 Τεύχος Α’
  issues and recorded 295 amending provisions across 51 FEKs (up to 03.11.2026).
- **Consolidated text:** for the 67 articles changed by consolidation, similarity to the Code's official wording
  rose from **0.70** (original 2017 text) to **0.91**. Unamended articles score 0.92 against the Code, because the
  Code itself makes editorial changes, so 0.92 is the practical ceiling. 61/67 articles moved closer; 29/67 are
  near-identical (≥ 0.95).
- **What it does not guess:** 310 changes were applied automatically and **16 were flagged** for manual review,
  with the reason and a link to the page. Examples: a phrase to replace that does not occur in the text, or a
  paragraph number that does not exist. Twenty non-textual changes (deadline extensions, suspensions) are
  listed separately.

## Where it runs

| Piece | Choice | Why |
|---|---|---|
| Code | Python package in a **private GitHub repository** | No server to maintain; full history of every change |
| Daily schedule | **GitHub Actions**, every evening and early morning (20:17 and 05:41 UTC) | Free for this workload (about 3 min/run); runs even when your PCs are off |
| Data | Plain JSON in `data/` committed by the bot | `git log` is an audit trail of what was found and when |
| Alerts | **A GitHub Issue per finding**, plus optional e-mail (SMTP) | GitHub e-mails everyone watching the repo; closing the issue marks it as reviewed |
| Reports | Static HTML in `site/`, optionally on **GitHub Pages** | Opens in any browser, also offline from a local clone |

The same code also runs on any computer (Windows/Mac/Linux), for example on an office PC with Task Scheduler.

## Setup (once)

1. Create an **empty private repository** on GitHub (for example `fek-watch`) and push this folder to it.
2. In the repo: **Actions** tab → enable workflows.
3. The repository already contains the full history for the three laws in `watchlist.toml`. For laws you
   add later: **Actions → Daily FEK check → Run workflow**, command `history`, and the act. This takes 15–40
   minutes for a heavily amended law, or run it locally.
4. Get notified: **Watch → Custom → Issues** on the repository, so you receive the e-mails GitHub sends.
5. Optional e-mail digest: **Settings → Secrets and variables → Actions** and add `SMTP_HOST`, `SMTP_PORT`
   (465 or 587), `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM` and `MAIL_TO` (comma-separated).
6. Optional website: **Settings → Pages → Source: GitHub Actions**, then add a repository *variable*
   `PAGES_ENABLED = true`. Note that Pages on a private repository needs a paid GitHub plan, and a Pages site
   is public. Without Pages, open `site/index.html` from a clone, or download it from the repo.

## Choosing the laws

Edit [`watchlist.toml`](watchlist.toml):

```toml
[[law]]
act = "ν. 4495/2017"
name = "Έλεγχος και προστασία του Δομημένου Περιβάλλοντος"
aliases = []          # names used instead of the number, e.g. for codes
```

Or use the command line: `python -m fekwatch watch add "ν. 4759/2020" --name "..."`. Then run `history` for
the new law. Supported acts: laws (ν.), presidential decrees (π.δ.), ν.δ., α.ν., β.δ. and ΠΝΠ.

## Local interface (search bar, watchlist, buttons)

Double-click **`start-ui.bat`** (Windows) or **`start-ui.command`** (Mac), or run `python -m fekwatch ui`.
A page opens at http://127.0.0.1:8765, reachable only from your own computer. From it you can:

- **search** every stored amendment and reference: free text ("αυθαίρετα", "πέργκολα"), a law
  ("ν. 4759/2020", which also offers to start watching it), or a FEK ("Α 101/2018"), with filters by law,
  article, year and type;
- **add or remove** watched laws and load their history with one click;
- run **"check new FEKs now"**, rebuild consolidated texts, or ask **"what does this FEK change?"**, with live
  progress shown on the page;
- open the amendment list and the consolidated text of each law.

## Using it locally

```bash
pip install -r requirements.txt   # Python 3.12+
```

```bash
python -m fekwatch history "ν. 4495/2017"      # full amendment history (cached; reruns are fast)
```

```bash
python -m fekwatch list "ν. 4495/2017"         # every amending FEK, by date
```

```bash
python -m fekwatch list "ν. 4495/2017" --format csv > 4495.csv
```

```bash
python -m fekwatch consolidate "ν. 4495/2017"  # consolidated text -> site/laws/n-4495-2017-consolidated.html
```

```bash
python -m fekwatch fek "Α 101/2018"            # analyse a single issue: what does it amend?
```

```bash
python -m fekwatch daily                       # what the scheduled job runs
```

```bash
python -m fekwatch history all --rebuild       # re-analyse everything after updating the parser
```

Then open `site/index.html`.

## How it works

```
search.et.gr API ──► new issues (released in the last 14 days, not yet processed)
        │
        ▼
PDF ──► text with layout (PyMuPDF): columns, headers, signatures removed, paragraphs rebuilt,
        │                           Latin look-alike letters fixed ("NOMOΣ" → "ΝΟΜΟΣ")
        ▼
acts → articles (bold headings) → «quoted new text» separated from the instruction
        │
        ▼
each provision: target act ("του ν. 4495/2017"), locator ("περ. β’ της παρ. 2 του άρθρου 5"),
                action (αντικαθίσταται / προστίθεται / καταργείται / τροποποιείται / διαγράφεται …)
        │
        ├──► watched act amended?  → record + alert
        ├──► only cited / derogated from / mentioned inside replaced text → record as reference
        └──► consolidation: operations applied to the original text, with provenance
```

Details that matter in practice:

- **Late releases.** ET often releases an issue days after its issue date. Each run re-checks the last 14
  days of *releases* and skips issues already processed.
- **Implicit references.** "13. Στο άρθρο 23 προστίθεται…" inside "Άρθρο 34 – Τροποποιήσεις του ν. 4495/2017"
  is attributed to 4495/2017. These findings are marked *συναγόμενο* (inferred).
- **Mentions that are not amendments.** When a law replaces the words «του ν. 4495/2017» *inside another
  law*, that is recorded as a reference, not an amendment of 4495/2017.
- **Decisions.** Τεύχος Β’, Δ’ and Α.Α.Π. are searched through ET's full-text index for decisions issued
  under a watched law. Those are listed as references (decisions cannot amend a law).
- **Cross-check.** The law page lists ET's own "Χρονολόγιο" links and flags Α’ issues that ET links but
  where no amendment was found, so they get a human look.

## Limitations (read before relying on it)

- This is an aid, **not an official consolidation**. Always confirm on the FEK; every finding links to the
  page.
- Consolidation applies amendments **in publication order**. It does not model deferred entry into force,
  retroactive effect, transitional provisions or annulments by courts.
- Amendments that refer to a law **only by name** (no number) are found only if you add that name to
  `aliases`.
- Very old, scanned FEKs (no text layer) are not supported. Issues from about 2000 onwards are born-digital
  and work.
- The ET search API is the one used by search.et.gr. It is public but undocumented and could change.
  `fekwatch/et_api.py` is the only place to adapt.
- ET's own amendment links are capped at 100 per FEK and are loosely curated, so they serve only as a
  cross-check.

## Project layout

```
fekwatch/
  et_api.py       search.et.gr API client (daily lists, full-text search, law → FEK, ET timeline, PDFs)
  pdftext.py      PDF → ordered paragraphs (layout-aware)
  normalize.py    Greek text normalisation (homoglyphs, hyphenation, accent folding)
  citations.py    citations ("ν. 4495/2017 (Α’ 167)"), locators (άρθρο/παρ./περ./εδάφιο), verbs
  amendments.py   issue → acts → articles → provisions (target, locator, action, quoted text)
  pipeline.py     daily job and history backfill
  consolidate.py  consolidation engine and its HTML
  report.py       static site (index, per-law pages, CSV)
  notify.py       GitHub issue and e-mail digest
watchlist.toml    what to watch
data/             findings (JSON, committed)
site/             generated HTML (committed)
tests/            unit tests on real FEK text
```

Licence note: PyMuPDF is AGPL-licensed, which is fine for internal use.
