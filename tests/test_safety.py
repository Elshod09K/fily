"""Safety tests. These are the tests that matter: every one of them is a
scenario in which the organizer must refuse to touch something.

Run with:  .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from organizer import safety


# --------------------------------------------------------------- folder validation

@pytest.mark.parametrize("bad", [
    "../../etc", "/etc/passwd", "~/Library", "..", ".",
    "a/../../b", "C:/Windows", "Foo\x00Bar", "Foo\nBar",
    "One/Two/Three/Four",                    # too deep
    "Evil.app", "Photos.photoslibrary",      # package bundles
    "Foo /Bar", "Foo./Bar", "", "   ",
])
def test_rejects_unsafe_folder_names(bad):
    ok, _ = safety.validate_relative_folder(bad)
    assert not ok, f"should have rejected {bad!r}"


@pytest.mark.parametrize("good", [
    "Academic", "Exams/SAT", "Academic/Dissertations/Drafts",
    "College Admissions", "Notes & Ideas", "Dr. Smith's PhD", "_Duplicates",
])
def test_accepts_reasonable_folder_names(good):
    ok, cleaned = safety.validate_relative_folder(good)
    assert ok, f"should have accepted {good!r}"
    assert not cleaned.startswith("/")


def test_outer_whitespace_is_normalised_not_rejected():
    ok, cleaned = safety.validate_relative_folder("  Academic/Exams  ")
    assert ok and cleaned == "Academic/Exams"


def test_traversal_cannot_escape_the_root(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    for attempt in ["../../../tmp", "..", "Foo/../../..", "/etc"]:
        dest, _ = safety.resolve_destination(root, attempt, "x.pdf", (root,))
        assert dest is None, f"{attempt!r} escaped the root"


def test_destination_must_be_inside_an_allowed_root(tmp_path):
    root, other = tmp_path / "Downloads", tmp_path / "Elsewhere"
    root.mkdir(); other.mkdir()
    dest, _ = safety.resolve_destination(root, "Academic", "x.pdf", (root,))
    assert dest is not None and root in dest.parents
    # the same folder resolved against a root that is not allowed
    dest, why = safety.resolve_destination(other, "Academic", "x.pdf", (root,))
    assert dest is None and "outside" in why


def _symlink_or_skip(link, target, is_dir=False):
    try:
        link.symlink_to(target, target_is_directory=is_dir)
    except OSError:
        pytest.skip("this account can't create symlinks (Windows without "
                    "Developer Mode)")


def test_symlinked_folder_cannot_smuggle_a_write_outside(tmp_path):
    root, outside = tmp_path / "Downloads", tmp_path / "Outside"
    root.mkdir(); outside.mkdir()
    _symlink_or_skip(root / "escape", outside, is_dir=True)
    dest, why = safety.resolve_destination(root, "escape", "x.pdf", (root,))
    assert dest is None, f"symlink escaped: {dest}"


# ------------------------------------------------------------------ atomic units

def test_git_repo_is_atomic(tmp_path):
    repo = tmp_path / "myproject"
    (repo / ".git").mkdir(parents=True)
    assert safety.project_marker(repo) == ".git"


@pytest.mark.parametrize("marker", ["package.json", "node_modules",
                                    "pyproject.toml", "Cargo.toml", "SKILL.md"])
def test_project_markers_detected(tmp_path, marker):
    d = tmp_path / "thing"
    d.mkdir()
    (d / marker).mkdir() if marker == "node_modules" else (d / marker).touch()
    assert safety.project_marker(d) == marker


@pytest.mark.parametrize("bundle", [
    "Photos Library.photoslibrary", "Thing.app", "Doc.rtfd", "X.musiclibrary",
])
def test_package_bundles_recognised(bundle):
    assert safety.is_bundle(Path(bundle))


# ------------------------------------------------------------------- scan roots

def test_home_itself_is_not_scannable():
    assert safety.root_rejection_reason(Path.home()) is not None


PROTECTED = (["AppData", "Pictures", "Music"] if sys.platform == "win32"
             else ["Library", "Pictures", "Movies", "Music", ".Trash"])


@pytest.mark.parametrize("p", PROTECTED)
def test_protected_roots_refused(p):
    from organizer import host
    root = (host.known_folder(p) if p in ("Pictures", "Music") else Path.home() / p)
    why = safety.root_rejection_reason(root)
    assert why is not None, f"{p} should not be scannable"


def test_a_whole_drive_is_refused():
    assert safety.root_rejection_reason(Path(Path.home().anchor)) is not None


def test_downloads_is_scannable():
    assert safety.root_rejection_reason(Path.home() / "Downloads") is None


# ------------------------------------------------------------------ file skipping

def _stat_of(p: Path):
    return p.lstat()


def test_recently_modified_file_is_quarantined(tmp_path):
    f = tmp_path / "working-on-this.docx"
    f.write_text("x")
    assert "quarantine" in safety.skip_reason(f, _stat_of(f), 24)


def test_old_file_is_eligible(tmp_path):
    f = tmp_path / "old.pdf"
    f.write_text("x")
    old = time.time() - 72 * 3600
    os.utime(f, (old, old))
    assert safety.skip_reason(f, _stat_of(f), 24) is None


def test_hardlink_is_skipped(tmp_path):
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"
    a.write_text("x")
    os.link(a, b)
    old = time.time() - 72 * 3600
    os.utime(b, (old, old))
    assert "hardlink" in safety.skip_reason(b, _stat_of(b), 24)


def test_symlink_is_skipped(tmp_path):
    real, link = tmp_path / "real.pdf", tmp_path / "link.pdf"
    real.write_text("x")
    _symlink_or_skip(link, real)
    assert safety.skip_reason(link, link.lstat(), 0) == "symlink"


@pytest.mark.parametrize("name", [
    ".DS_Store", ".localized", "~$draft.docx", "._resourcefork", "Icon\r",
])
def test_metadata_files_ignored(name):
    assert safety.is_ignored_name(name)


@pytest.mark.parametrize("name", ["big.dmg.crdownload", "half.part", "x.tmp"])
def test_partial_downloads_skipped(tmp_path, name):
    f = tmp_path / name
    f.write_text("x")
    old = time.time() - 72 * 3600
    os.utime(f, (old, old))
    assert "transient" in safety.skip_reason(f, _stat_of(f), 24)


# --------------------------------------------------------------- no overwriting

def test_collisions_never_overwrite(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_text("original")
    assert safety.unique_destination(f).name == "report (2).pdf"
    (tmp_path / "report (2).pdf").write_text("second")
    assert safety.unique_destination(f).name == "report (3).pdf"


def test_free_name_is_returned_unchanged(tmp_path):
    f = tmp_path / "fresh.pdf"
    assert safety.unique_destination(f) == f


# ------------------------------------------------------- non-English folders

@pytest.mark.parametrize("name", [
    "Шартномалар", "Oʻquv reja", "Dissertatsiya/2026", "Études", "文档",
])
def test_folders_in_any_language_are_allowed(name):
    ok, cleaned = safety.validate_relative_folder(name)
    assert ok, cleaned


@pytest.mark.parametrize("name,why", [
    ("a∕b", "division slash that looks like /"),
    ("a／b", "fullwidth slash"),
    ("invoice‮fdp.exe", "right-to-left override that disguises a name"),
    ("a​b", "zero-width space"),
    ("a b", "no-break space inside a name"),
    ("a\tb", "tab"),
    ("Con", "reserved device name on Windows"),
    ("LPT1.backup", "reserved device name with an extension"),
])
def test_unicode_tricks_are_rejected(name, why):
    ok, _ = safety.validate_relative_folder(name)
    assert not ok, why


def test_one_canonical_spelling():
    """'é' precomposed and 'e' + combining accent must be the same folder."""
    composed = safety.validate_relative_folder("Café")[1]
    decomposed = safety.validate_relative_folder("Café")[1]
    assert composed == decomposed


@pytest.mark.parametrize("name", ["Desktop App.lnk", "site.url", "tool.appref-ms"])
def test_shortcuts_are_left_alone(tmp_path, name):
    f = tmp_path / name
    f.write_text("x")
    old = time.time() - 72 * 3600
    os.utime(f, (old, old))
    assert safety.skip_reason(f, f.lstat(), 24) == "shortcut"
