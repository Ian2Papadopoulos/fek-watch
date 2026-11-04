"""Keep a local copy in step with the GitHub repository.

The daily GitHub run is the official record: it commits the findings (data/) and the reports (site/).
Running the app locally rewrites the same files, so a plain `git pull` would refuse to run. Sync handles it:

* the watchlist is the one thing a person edits locally; it is committed and pushed, so the daily
  run on GitHub follows the same laws;
* the history of a newly added law that was loaded locally (a new data/laws/<slug>.json) is pushed with
  it, which saves the GitHub run from repeating the scan;
* every other local change under data/ and site/ is a regenerable preview and is dropped in favour of
  the GitHub copy;
* if the watchlist changed on both sides, the result is the union of the two lists.

Nothing here raises for the expected failures (no git, offline, no credentials): it returns messages.
"""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path

from . import config as cfgmod

WATCHLIST = "watchlist.toml"


class GitError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True, timeout: int = 120) -> subprocess.CompletedProcess:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        p = subprocess.run(["git", *args], cwd=str(root), capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=timeout)
    except FileNotFoundError as exc:
        raise GitError("Δεν βρέθηκε το git στον υπολογιστή (https://git-scm.com/download).") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"Το git {' '.join(args[:1])} δεν απάντησε εγκαίρως (σύνδεση;).") from exc
    if check and p.returncode != 0:
        raise GitError((p.stderr or p.stdout).strip() or f"git {' '.join(args)} failed")
    return p


def is_repo(root: Path) -> bool:
    try:
        return _git(root, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() == "true"
    except GitError:
        return False


# --------------------------------------------------------------------------- watchlist union

def _law_blocks(text: str) -> list[tuple[str, str]]:
    """[(act key, raw block text)] for every [[law]] block, in file order."""
    out = []
    for part in re.split(r"(?m)^(?=\[\[law\]\])", text):
        if not part.startswith("[[law]]"):
            continue
        try:
            data = tomllib.loads(part)
            act = data["law"][0]["act"]
        except (tomllib.TOMLDecodeError, KeyError, IndexError):
            continue
        out.append((cfgmod.watched_from(act).key, part.rstrip() + "\n"))
    return out


def merge_watchlists(base_text: str, other_text: str) -> str:
    """`base_text` plus every [[law]] of `other_text` that it does not already watch."""
    have = {k for k, _ in _law_blocks(base_text)}
    extra = [blk for k, blk in _law_blocks(other_text) if k not in have]
    if not extra:
        return base_text
    return base_text.rstrip() + "\n\n" + "\n".join(extra)


def watchlist_keys(text: str) -> list[str]:
    return [k for k, _ in _law_blocks(text)]


# --------------------------------------------------------------------------- steps

def _changed(root: Path, path: str) -> bool:
    return bool(_git(root, "status", "--porcelain", "--", path).stdout.strip())


def _new_law_files(root: Path) -> list[str]:
    """Untracked data/laws/*.json of watched laws (history loaded locally for a newly added law)."""
    out = _git(root, "ls-files", "--others", "--exclude-standard", "--", "data/laws").stdout.split()
    try:
        slugs = {w.slug for w in cfgmod.load(root).laws}
    except Exception:  # noqa: BLE001
        return []
    return [p for p in out if p.endswith(".json") and Path(p).stem in slugs]


def _describe(root: Path) -> str:
    """'Watchlist: + ν. 1/2020, − π.δ. 2/2019' for the commit message."""
    old = _git(root, "show", f"HEAD:{WATCHLIST}", check=False).stdout
    new = (root / WATCHLIST).read_text(encoding="utf-8")
    a, b = set(watchlist_keys(old)), set(watchlist_keys(new))
    parts = [f"+ {k}" for k in sorted(b - a)] + [f"- {k}" for k in sorted(a - b)]
    return "Watchlist: " + (", ".join(parts) if parts else "edited")


def commit_local(root: Path) -> str | None:
    """Commit the watchlist and the history files of newly added laws. Returns the commit message or None."""
    files = ([WATCHLIST] if _changed(root, WATCHLIST) else []) + _new_law_files(root)
    if not files:
        return None
    msg = _describe(root) if WATCHLIST in files else "History of newly added laws"
    _git(root, "add", "--", *files)
    _git(root, "-c", "user.name=fek-watch (local)", "-c", "user.email=fek-watch@users.noreply.github.com",
         "commit", "-q", "-m", msg, "--", *files)
    return msg


def discard_generated(root: Path) -> bool:
    """Drop local changes to data/ and site/ (except files just committed). True if anything was dropped."""
    dirty = _git(root, "status", "--porcelain", "--", "data", "site").stdout.strip()
    if not dirty:
        return False
    _git(root, "checkout", "--", "data", "site", check=False)
    _git(root, "clean", "-fdq", "--", "data", "site", check=False)
    return True


def _resolve_conflicts(root: Path) -> None:
    unmerged = _git(root, "diff", "--name-only", "--diff-filter=U").stdout.split()
    if not unmerged:
        raise GitError("merge failed")
    for path in unmerged:
        if path == WATCHLIST:
            ours = _git(root, "show", f":2:{WATCHLIST}", check=False).stdout
            theirs = _git(root, "show", f":3:{WATCHLIST}", check=False).stdout
            (root / WATCHLIST).write_text(merge_watchlists(theirs, ours), encoding="utf-8")
        elif path.startswith(("data/", "site/")):
            _git(root, "checkout", "--theirs", "--", path)   # GitHub copy is the record
        else:
            _git(root, "merge", "--abort", check=False)
            raise GitError(f"Σύγκρουση στο {path}: χρειάζεται χειροκίνητη επίλυση (git status).")
        _git(root, "add", "--", path)
    _git(root, "-c", "user.name=fek-watch (local)", "-c", "user.email=fek-watch@users.noreply.github.com",
         "commit", "-q", "--no-edit")


def pull(root: Path) -> str:
    before = _git(root, "rev-parse", "HEAD").stdout.strip()
    p = _git(root, "-c", "user.name=fek-watch (local)", "-c", "user.email=fek-watch@users.noreply.github.com",
             "pull", "--no-rebase", "--no-edit", "-q", check=False, timeout=300)
    if p.returncode != 0:
        if _git(root, "diff", "--name-only", "--diff-filter=U").stdout.strip():
            _resolve_conflicts(root)
        else:
            raise GitError((p.stderr or p.stdout).strip())
    after = _git(root, "rev-parse", "HEAD").stdout.strip()
    if before == after:
        return "Ήδη ενημερωμένο."
    n = _git(root, "rev-list", "--count", f"{before}..{after}").stdout.strip()
    return f"Ενημερώθηκε από το GitHub ({n} νέες καταχωρίσεις)."


def ahead(root: Path) -> int:
    p = _git(root, "rev-list", "--count", "@{u}..HEAD", check=False)
    return int(p.stdout.strip()) if p.returncode == 0 and p.stdout.strip().isdigit() else 0


def push(root: Path) -> None:
    _git(root, "push", "-q", timeout=300)


# --------------------------------------------------------------------------- entry points

def sync(root: Path | str = ".") -> list[str]:
    """Commit local watchlist edits, drop local previews, pull, push. Returns messages for the user."""
    root = Path(root).resolve()
    msgs: list[str] = []
    if not is_repo(root):
        return ["Ο φάκελος δεν είναι αντίγραφο git· παράλειψη συγχρονισμού."]
    try:
        c = commit_local(root)
        if c:
            msgs.append(f"Αποθηκεύτηκαν οι τοπικές αλλαγές: {c}")
        if discard_generated(root):
            msgs.append("Τα τοπικά αποτελέσματα (data/, site/) αντικαταστάθηκαν από τα επίσημα του GitHub.")
        msgs.append(pull(root))
        if ahead(root):
            push(root)
            msgs.append("Η λίστα παρακολούθησης στάλθηκε στο GitHub: ο καθημερινός έλεγχος θα την ακολουθεί.")
    except GitError as exc:
        msgs.append(f"Ο συγχρονισμός με το GitHub δεν ολοκληρώθηκε: {exc}")
    return msgs


def push_watchlist(root: Path | str = ".") -> tuple[bool, str]:
    """After the app edits the watchlist: commit it and send it to GitHub. (ok, message)"""
    root = Path(root).resolve()
    if not is_repo(root):
        return False, "Ο φάκελος δεν είναι αντίγραφο git: η αλλαγή έμεινε μόνο σε αυτόν τον υπολογιστή."
    try:
        commit_local(root)
        if not ahead(root):
            return True, "Καμία αλλαγή προς αποστολή."
        p = _git(root, "push", "-q", check=False, timeout=300)
        if p.returncode != 0:          # GitHub moved on (the daily run): pull then push again
            discard_generated(root)
            pull(root)
            push(root)
        return True, "Στάλθηκε στο GitHub: ο καθημερινός έλεγχος θα το παρακολουθεί."
    except GitError as exc:
        return False, f"Αποθηκεύτηκε τοπικά, αλλά δεν στάλθηκε στο GitHub: {exc}"
