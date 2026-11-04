# FEK Watch

Παρακολουθεί καθημερινά την Εφημερίδα της Κυβερνήσεως (ΦΕΚ) και εντοπίζει τις διατάξεις που τροποποιούν τους
νόμους, τα διατάγματα και τις υπουργικές αποφάσεις μιας λίστας. Για κάθε πράξη κρατά όλα τα ΦΕΚ που την έχουν
τροποποιήσει και φτιάχνει ενοποιημένο κείμενο. Κάθε αλλαγή παραπέμπει στη σελίδα του ΦΕΚ, ώστε να ελέγχεται.

FEK Watch reads the Greek Government Gazette (ΦΕΚ) every day and finds the provisions that amend the acts on a
watch list.

Every day it fetches the newly released gazette issues, finds provisions that replace, add to or repeal anything
in a watched act, and opens an alert.

For each act it keeps the full amendment history since publication: which article of which amending law changed
which article or paragraph, with a link to the exact PDF page. The list is cross-checked against the Printing
Office's own (incomplete) links.

It also builds a consolidated text, which is experimental. The amendments are applied to the original text in
publication order; every changed paragraph has a footnote pointing to the amending FEK page and keeps its
previous wording. Changes it cannot apply with certainty are listed for manual review and left out of the text.

## How accurate it is

The test case is ν. 4495/2017. The Spatial Planning Code (ν. 5306/2026, ΦΕΚ Α’ 88) codified it, and its
official correspondence table lists the laws that amended each provision. The Code also re-enacts the
consolidated wording, so it serves as an answer key.

- All 39 amending laws in the official table were found: 38 as textual amendments, and one that sets up a
  separate regime, recorded as a derogation. The history scan checked 134 issues of Τεύχος Α’ and recorded 295
  amending provisions across 51 FEKs (up to 03.11.2026).
- In the 67 articles that consolidation changed, similarity to the Code's wording rose from 0.70 for the 2017
  text to 0.91. Unamended articles score 0.92 against the Code, because the Code makes its own editorial changes,
  so 0.92 is the practical ceiling. 61 of the 67 articles moved closer, and 29 are near-identical (0.95 or more).
- 309 changes were applied automatically and 17 were flagged for manual review, each with the reason and a link
  to the page. Typical reasons are a phrase to replace that does not occur in the text, or a paragraph number
  that does not exist. Twenty non-textual changes, such as deadline extensions and suspensions, are listed
  separately.

## How it works

### The scheduled check

The check runs on GitHub Actions (`.github/workflows/daily.yml`), so no computer needs to stay on. It is
scheduled twice a day: at 20:17 UTC, after the day's issues are out, and at 05:41 UTC, for issues uploaded late
in the evening. GitHub treats schedules as best effort, so a run may start late and is occasionally skipped. It
can also be started by hand from **Actions → Daily FEK check → Run workflow**.

Each run:

1. loads the full amendment history of any act that was added to the watch list since the last run;
2. lists the issues released in the last 14 days and skips those already processed (the Printing Office often
   releases an issue days after its issue date);
3. analyses the new issues of Τεύχος Α’, and searches Τεύχη Β’, Δ’ and Α.Α.Π. for decisions that cite a watched
   act (and for the amendments of watched ministerial decisions);
4. sends the alerts (below);
5. rebuilds the consolidated texts and the HTML reports;
6. commits the results to `data/` and `site/`, so `git log` shows what was found and when.

### Alerts

When a run finds something, the bot opens one GitHub Issue for that run, labelled `fek-watch`. The issue groups
the findings by act and by FEK and links each one to its page. It covers amendments and derogations, and also
references: decisions and other acts that cite a watched act without changing it.

GitHub e-mails everyone who watches the repository, so the way to receive alerts is **Watch → Custom →
Issues**. Closing an issue marks it as reviewed.

An e-mail digest with the same content is sent as well when these repository secrets exist
(**Settings → Secrets and variables → Actions**): `SMTP_HOST`, `SMTP_PORT` (465 or 587), `SMTP_USER`,
`SMTP_PASSWORD`, `MAIL_FROM` and `MAIL_TO` (comma-separated).

The local interface also marks laws and FEKs that are new since it was last opened on that computer.

### Reading a FEK

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

Some details that matter in practice:

- "13. Στο άρθρο 23 προστίθεται…" inside "Άρθρο 34 – Τροποποιήσεις του ν. 4495/2017" is attributed to
  ν. 4495/2017. Findings like this are marked *συναγόμενο* (inferred).
- When a law replaces the words «του ν. 4495/2017» inside another law, that is recorded as a reference to
  4495/2017, since its own text does not change.
- About one FEK in ten embeds Calibri or Arial without a character map, so a PDF reader sees glyph numbers
  instead of letters, mostly in tables. FEK Watch draws each glyph and matches it, by width and shape, against
  Carlito and Arimo, free fonts with the same widths (`fekwatch/glyphmap.py`). A font is decoded only when most
  glyphs match closely; otherwise the text is shown as «[μη αναγνώσιμο κείμενο, βλ. σελ. N του ΦΕΚ]».
- When an amending FEK gives the full new wording of a provision ("… και το άρθρο 199 διαμορφώνεται ως εξής:
  «…»"), that wording is applied as a whole and the listed changes are treated as a summary. A provision is
  never applied in part: if one step cannot be understood, none is applied and the provision goes to manual
  review. An act republished as a whole by a decision is listed with a link to the new text.
- The page of each law lists the Printing Office's own "Χρονολόγιο" links and flags issues of Τεύχος Α’ that
  it links but where no amendment was found, so someone can look at them.

## Setting up a copy

1. Get a copy of this repository on GitHub, either from **Use this template** (when the original has it enabled)
   or by pushing a clone to a new repository. See [Sharing](#sharing-it) for the choices.
2. Open the **Actions** tab and enable workflows.
3. Edit the watch list (below). The next scheduled run loads the history of every act that has none. To load it
   at once, run the workflow by hand with command `history` and the act; a heavily amended law takes 15 to 40
   minutes.
4. Turn on alerts: **Watch → Custom → Issues**, and optionally the e-mail secrets listed under
   [Alerts](#alerts).
5. Optional website: **Settings → Pages → Source: GitHub Actions**, then a repository variable
   `PAGES_ENABLED = true`.

A public repository shows everything in it to anyone: the code, the findings, the watch list and the alert
issues. The e-mail secrets stay hidden either way. A private repository keeps all of that to the people it is
shared with. On GitHub's free plan a private repository gets 2,000 Actions minutes a month; this workload uses a
few hundred, plus 30 to 60 minutes each time a long history loads. GitHub Pages from a private repository needs
a paid plan, and the site it publishes is public.

## The watch list

[`watchlist.toml`](watchlist.toml) lists the watched acts:

```toml
[[law]]
act = "ν. 4495/2017"
name = "Έλεγχος και προστασία του Δομημένου Περιβάλλοντος"
aliases = []          # names used instead of the number, e.g. for codes
```

Acts can also be added from the local interface or with `python -m fekwatch watch add "ν. 4759/2020" --name "..."`.
Supported acts are laws (ν.), presidential decrees (π.δ.), ν.δ., α.ν., β.δ., ΠΝΠ and ministerial decisions. A
decision is written with its protocol number and year:

```toml
[[law]]
act = "ΥΑ ΥΠΕΝ/ΔΑΟΚΑ/66006/2360/2023"
name = "Κτιριοδομικός Κανονισμός"
fek = "Β 3985/2023"        # optional: found automatically otherwise
```

For a decision, Τεύχος Β’ is searched too, since decisions are mostly amended by other decisions, and only the
issues that mention its protocol number are downloaded.

## The local interface

Double-click `start-ui.bat` (Windows) or `start-ui.command` (Mac), or run `python -m fekwatch ui`. A page opens
at http://127.0.0.1:8765 and is reachable only from that computer. It has:

- a search over every stored amendment and reference, by free text ("αυθαίρετα", "πέργκολα"), by act
  ("ν. 4759/2020", which also offers to start watching it) or by FEK ("Α 101/2018"), with filters for act,
  article, year and type of finding;
- the watched acts, with their amendment list, consolidated text and the most recent amending FEKs;
- buttons to add or remove an act and load its history, check new FEKs now, rebuild the consolidated texts, or
  show what a given FEK changes, with progress shown on the page.

Clicking a link to a FEK opens a small menu: «Προβολή σε νέα καρτέλα» shows the PDF in a new tab at the right
page, and «Λήψη PDF» saves it (for example `FEK-A-122-2026.pdf`). The report pages do the same.

### Staying in step with GitHub

The launchers run `python -m fekwatch sync` before opening the page, and the «Ενημέρωση» button under
«Τελευταίος έλεγχος» runs it on demand. Sync:

- commits and pushes changes to the watch list, so the scheduled check follows the same acts;
- also pushes the history of a newly added act that was loaded on that computer, which saves the scheduled run
  from repeating the scan;
- replaces local results in `data/` and `site/` with the ones on GitHub;
- pulls, and keeps the acts of both sides if the watch list changed on both.

Results produced locally, such as «Έλεγχος νέων ΦΕΚ», are a preview. The scheduled run on GitHub is the record,
and the next sync replaces the preview with it. Use `sync` (or the launcher) instead of a plain `git pull`,
which stops when the app has rewritten files that the bot also changed.

Adding or removing an act in the app pushes the watch list straight away. That needs the computer to be signed
in to GitHub with write access; otherwise the change stays on that computer and the page says so.

## Sharing it

### Colleagues on the same watch list

Add them under **Settings → Collaborators** with the Write role. Each person installs Python 3.12 or newer and
Git, clones the repository and opens `start-ui.bat`, and turns on **Watch → Issues** to receive the alerts.
Everyone shares one watch list: an act one person adds is followed for all. With the Read role a person can use
the app and receive alerts, but their watch-list changes stay on their own computer.

### Independent copies

For a separate watch list and separate alerts, each person or office needs its own repository:

1. In the original repository, the owner ticks **Settings → General → Template repository**.
2. The new user opens it, chooses **Use this template → Create a new repository**, and can make it private. The
   copy includes the code and the data loaded so far.
3. In the copy: enable workflows in **Actions**, edit the watch list, and turn on **Watch → Issues** and, if
   wanted, the e-mail secrets.

A copy has its own schedule, data and alerts. It does not receive later changes to the original automatically.
To bring them in:

```bash
git remote add upstream https://github.com/Ian2Papadopoulos/fek-watch.git
```

```bash
git pull upstream main
```

If the watch list conflicts, keep the copy's own `[[law]]` blocks.

### Read-only access to the reports

With GitHub Pages enabled (see [Setting up a copy](#setting-up-a-copy)), the reports get a web address that
opens without a GitHub account. The `site/` folder can also be sent as it is; its pages open in any browser.

### Without GitHub

The program also runs from a downloaded ZIP. Checks then happen only when someone presses «Έλεγχος νέων ΦΕΚ»,
or on a schedule set up with Windows Task Scheduler running `python -m fekwatch daily`. There are no issue
alerts; the e-mail digest works when the `SMTP_*` variables listed under [Alerts](#alerts) are set as
environment variables on that computer.

## Command line

```bash
pip install -r requirements.txt   # Python 3.12+
```

```bash
python -m fekwatch history "ν. 4495/2017"      # full amendment history (cached; reruns are fast)
```

```bash
python -m fekwatch history all --missing       # only acts whose history was never loaded
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
python -m fekwatch sync                        # send watch-list changes to GitHub, fetch the official results
```

```bash
python -m fekwatch history all --rebuild       # re-analyse everything after updating the parser
```

The reports are in `site/index.html`.

## Limitations

- This is an aid, not an official consolidation. Confirm every finding on the FEK; each one links to its page.
- Consolidation applies amendments in publication order. It does not model deferred entry into force,
  retroactive effect, transitional provisions or annulments by courts.
- Amendments that name a law without its number are found only if the name is in `aliases`.
- Scanned FEKs with no text layer are not supported. Issues from about 2000 onwards are born-digital and work.
- Text in a font that matches neither Calibri nor Arial, and has no character map, is marked as unreadable.
- The ET search API is the one search.et.gr uses. It is public but undocumented and could change;
  `fekwatch/et_api.py` is the only place to adapt.
- The Printing Office's own amendment links stop at 100 per FEK and are loosely curated, so they serve only as a
  cross-check.

## Project layout

```
fekwatch/
  et_api.py       search.et.gr API client (daily lists, full-text search, law → FEK, ET timeline, PDFs)
  pdftext.py      PDF → ordered paragraphs (layout-aware)
  glyphmap.py     letters for embedded fonts that have no character map
  normalize.py    Greek text normalisation (homoglyphs, hyphenation, accent folding)
  citations.py    citations ("ν. 4495/2017 (Α’ 167)"), locators (άρθρο/παρ./περ./εδάφιο), verbs
  amendments.py   issue → acts → articles → provisions (target, locator, action, quoted text)
  pipeline.py     daily job and history backfill
  consolidate.py  consolidation engine and its HTML
  report.py       static site (index, per-law pages, CSV)
  notify.py       GitHub issue and e-mail digest
  sync.py         keeps a local copy in step with GitHub
  ui.py           local web interface (server); ui_page.html is the page
  pdflinks.py     the preview / download menu on FEK links
  fonts/          Carlito and Arimo (SIL Open Font Licence) for glyphmap.py
watchlist.toml    what to watch
data/             findings (JSON, committed by the bot)
site/             generated HTML (committed by the bot)
tests/            unit tests on real FEK text
```

PyMuPDF is AGPL-licensed, which is fine for internal use. Carlito and Arimo are under the SIL Open Font
Licence; see `fekwatch/fonts/`.
