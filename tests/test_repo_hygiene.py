"""Repo hygiene: no internal infrastructure identifiers in tracked files.

beherouter is public. The scrub that retired the homelab's addresses, hostnames,
repo path, credential-ledger filename and vault key names was a one-time edit; THIS is what keeps
it true. Same enforcement style the plugins already use for the Plane Community
Edition 404s and the Microsoft `consumers` authority: a trap held by a test
rather than by prose, because prose does not fail a build.

Scope is deliberately TRACKED files only. Untracked working directories
(docs/superpowers/plans/, docs/handoffs/, local-infra/) are excluded from
publication by .gitignore, not by rewriting — see
test_internal_working_directories_are_gitignored below, which is what makes that
exclusion load-bearing rather than incidental.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN = {
    "homelab subnet (223)": re.compile(r"192\.168\.223\.\d{1,3}"),
    "homelab subnet (88)": re.compile(r"192\.168\.88\.\d{1,3}"),
    "private domain": re.compile(r"[A-Za-z0-9-]*\bmezin\.lu\b"),
    "homelab repo path": re.compile(r"Srv/homelab"),
    "credential ledger": re.compile(r"passwords\.txt"),
    "absolute home path": re.compile(r"/Users/alexandr"),
}

# `{{ vault_<name> }}` is the jinja placeholder scheme `plugin-config` emits for
# an operator's env template — the 2026-09-11 baseline scan declared it clean.
# A BARE key name in prose is an identifier leak, so it is checked separately,
# against the text with the blessed placeholders masked out.
_VAULT_PLACEHOLDER = re.compile(r"\{\{ vault_[a-z0-9_]+ \}\}")
_VAULT_NAME = re.compile(r"vault_[a-z0-9_]+")

# Text formats only. uv.lock is skipped because it is a machine-generated wall
# of hashes and URLs that no human edits, and scanning it is pure false-positive
# surface. This module is skipped because it necessarily contains the patterns
# it forbids.
SCANNED_SUFFIXES = {".md", ".py", ".toml", ".yml", ".yaml", ".json", ".j2", ".sh", ".txt"}
SKIPPED = {"uv.lock", "tests/test_repo_hygiene.py"}


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    )
    return out.stdout.splitlines()


def test_no_internal_identifiers_in_tracked_files():
    offenders: list[str] = []
    for rel in _tracked_files():
        if rel in SKIPPED or Path(rel).suffix not in SCANNED_SUFFIXES:
            continue
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        for label, pattern in FORBIDDEN.items():
            for match in pattern.finditer(text):
                line = text[: match.start()].count("\n") + 1
                offenders.append(f"{rel}:{line} — {label} — {match.group(0)!r}")
    assert not offenders, "internal identifiers must not be published:\n" + "\n".join(offenders)


def test_no_bare_vault_key_names_in_tracked_files():
    """`{{ vault_... }}` placeholders are plugin-config's emitted scheme; bare
    key names in prose are infrastructure identifiers."""
    offenders: list[str] = []
    for rel in _tracked_files():
        if rel in SKIPPED or Path(rel).suffix not in SCANNED_SUFFIXES:
            continue
        text = (REPO_ROOT / rel).read_text(encoding="utf-8", errors="replace")
        masked = _VAULT_PLACEHOLDER.sub(" ", text)
        for match in _VAULT_NAME.finditer(masked):
            line = masked[: match.start()].count("\n") + 1
            offenders.append(f"{rel}:{line} — vault key name — {match.group(0)!r}")
    assert not offenders, "internal identifiers must not be published:\n" + "\n".join(offenders)


def test_internal_working_directories_are_gitignored():
    """Untracked is not the same as safe: one `git add .` publishes forever."""
    for path in ("docs/superpowers/plans/", "docs/handoffs/", "local-infra/"):
        # check=False is deliberate: `git check-ignore -q` exits 1 when the path
        # is NOT ignored, which is exactly the case this assertion exists to catch.
        result = subprocess.run(
            ["git", "check-ignore", "-q", path], cwd=REPO_ROOT, check=False
        )
        assert result.returncode == 0, f"{path} must be gitignored, not merely untracked"


def test_licence_and_notice_are_present_and_name_the_attribution():
    """Apache 2.0 §4(d) only binds downstream if the NOTICE actually says who to credit."""
    assert (REPO_ROOT / "LICENSE").is_file(), "LICENSE is missing"
    notice = (REPO_ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "Behemotion" in notice
    assert "https://behemotion.com" in notice
    assert "Aleksandr Mezin" in notice
