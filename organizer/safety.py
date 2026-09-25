"""Hard safety rules. Not configurable.

Everything here is deliberately conservative: when a rule is unsure, it excludes.
A file wrongly skipped is a non-event; a file wrongly moved is a bug report.
"""
from __future__ import annotations

import os
import time
import unicodedata
from pathlib import Path

from . import host

HOME = Path.home()

# Never touched, under any configuration. The lists are per-OS (see host/):
# ~/Library and the system folders on a Mac; Windows, Program Files, AppData
# and friends on Windows.
HARD_DENY_ROOTS: tuple[Path, ...] = host.protected_roots()

# Destination-only: loose media is routed *into* these, and on a Mac they
# hold Apple-managed library bundles.
LIBRARY_ROOTS: tuple[Path, ...] = host.library_roots()

# Marker files that make a directory an atomic project: never descend, never move
# individual members.
PROJECT_MARKERS: frozenset[str] = frozenset({
    ".git", "node_modules", "package.json", "pyproject.toml", "Cargo.toml",
    "go.mod", ".venv", "venv", "Gemfile", "pom.xml", "build.gradle",
    ".terraform", "requirements.txt", "SKILL.md", ".claude-plugin",
})

# macOS package bundles: these look like directories but are single documents.
BUNDLE_SUFFIXES: frozenset[str] = frozenset({
    ".app", ".photoslibrary", ".musiclibrary", ".tvlibrary", ".photobooth",
    ".rtfd", ".fcpbundle", ".sparsebundle", ".bundle", ".framework", ".pkg",
    ".xcodeproj", ".xcworkspace", ".playground", ".imovielibrary", ".theater",
    ".logicx", ".band", ".key", ".pages", ".numbers", ".aplibrary",
    ".migratedphotolibrary", ".pvm", ".scptd", ".prefPane", ".qlgenerator",
})

# Never moved, never counted, never reported.
IGNORED_NAMES: frozenset[str] = frozenset({
    ".DS_Store", ".localized", "Icon\r", "Icon", ".gitkeep", "Thumbs.db",
    ".Spotlight-V100", ".fseventsd", ".TemporaryItems", ".apdisk",
    ".com.apple.timemachine.donotpresent", "desktop.ini",
})

# In-flight downloads and editor scratch files.
TRANSIENT_SUFFIXES: frozenset[str] = frozenset({
    ".crdownload", ".part", ".partial", ".download", ".tmp", ".temp",
    ".swp", ".swo", ".lock", ".!qb", ".opdownload", ".aria2",
})

TRANSIENT_PREFIXES: tuple[str, ...] = ("~$", ".~lock.", "._")

# Shortcuts. A Windows Desktop is full of them, and tidying someone's app
# shortcuts into a folder is not what they asked for.
SHORTCUT_SUFFIXES: frozenset[str] = frozenset({".lnk", ".url", ".appref-ms"})

# Names Windows reserves for devices; a folder called "Con" can't exist there,
# and a Mac folder with such a name breaks when synced to a Windows machine.
WINDOWS_RESERVED: frozenset[str] = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)})

# Folder names the model may propose: conservative charset, no traversal.
# Folder names the model may propose. Letters and digits from any script are
# fine — Uzbek or Russian users get folders in their own language — so the
# rule is by Unicode category rather than an ASCII allow-list:
#   letters (L*), combining marks (M*), decimal digits (Nd), plain space and
#   _ . - & ( ) '  — and nothing else.
# That still rejects everything that matters for safety: control and format
# characters (so no NUL, newline, zero-width or right-to-left override, which
# can disguise a name), every separator but a plain space, and look-alike
# slashes such as U+2215 and U+FF0F, which are symbols (S*) or punctuation (P*).
_SEGMENT_PUNCT = frozenset(" _.-&()'")
_SEGMENT_MAX = 49
MAX_FOLDER_DEPTH = 3


def _is_under(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def under_any(path: Path, parents) -> bool:
    return any(_is_under(path, p) for p in parents)


def root_rejection_reason(root: Path, cfg=None) -> str | None:
    """Why this directory may not be used as a scan root, or None if it may."""
    if not root.is_absolute():
        return "not an absolute path"
    if root == HOME:
        return "the home directory itself is too broad to scan"
    if root == Path(root.anchor):
        return "a whole drive is never scannable"
    if under_any(root, HARD_DENY_ROOTS):
        return "inside a system or library location"
    if any(_is_under(p, root) for p in HARD_DENY_ROOTS):
        return "contains system or application folders"
    for lib in LIBRARY_ROOTS:
        if _is_under(root, lib):
            return (f"{lib.name} is where sorted media goes, so it is a "
                    "destination, never a scan root")
    if is_bundle(root):
        return "this is a macOS package bundle, not a folder"
    if project_marker(root):
        return "this directory is a code project and is treated as atomic"
    return None


def is_bundle(path: Path) -> bool:
    return path.suffix.lower() in BUNDLE_SUFFIXES


def project_marker(directory: Path) -> str | None:
    """Return the marker that makes `directory` an atomic project, if any."""
    try:
        with os.scandir(directory) as it:
            for entry in it:
                if entry.name in PROJECT_MARKERS:
                    return entry.name
    except (PermissionError, NotADirectoryError, FileNotFoundError, OSError):
        return None
    return None


def is_ignored_name(name: str) -> bool:
    if name in IGNORED_NAMES:
        return True
    return name.startswith(TRANSIENT_PREFIXES)


def skip_reason(path: Path, st: os.stat_result, quarantine_hours: int) -> str | None:
    """Why this *file* must not be moved, or None if it is eligible."""
    name = path.name
    if is_ignored_name(name):
        return "system metadata file"
    if host.hidden(name, st):
        return "hidden file"
    if path.suffix.lower() in TRANSIENT_SUFFIXES:
        return "transient or in-flight file"
    if path.suffix.lower() in SHORTCUT_SUFFIXES:
        return "shortcut"
    if path.is_symlink():
        return "symlink"
    cloud = host.cloud_only(st)
    if cloud:
        return cloud
    if st.st_nlink > 1:
        return f"hardlinked ({st.st_nlink} links)"
    if st.st_size == 0:
        return "empty file"
    age_hours = (time.time() - st.st_mtime) / 3600.0
    if age_hours < quarantine_hours:
        return f"modified {age_hours:.1f}h ago (quarantine {quarantine_hours}h)"
    if not os.access(path, os.R_OK):
        return "not readable"
    return None


def is_file_open(path: Path) -> bool:
    """True if another process currently holds the file open (lsof on a Mac,
    an exclusive-open probe on Windows). Checked immediately before a move."""
    return host.is_file_open(path)


def validate_relative_folder(folder: str) -> tuple[bool, str]:
    """Validate a model-proposed folder name.

    The model never supplies a path we execute against; this gate is what makes
    a prompt injection in a filename or PDF harmless.
    """
    if not folder or not folder.strip():
        return False, "empty"
    # One canonical spelling: macOS stores decomposed forms, so "é" typed two
    # ways would otherwise become two different-looking-identical folders.
    f = unicodedata.normalize("NFC", folder.strip()).replace("\\", "/")
    if f.startswith("/") or f.startswith("~"):
        return False, "absolute path"
    if ":" in f:
        return False, "contains a volume separator"
    if "\x00" in f or "\n" in f or "\r" in f:
        return False, "contains a control character"
    segments = [s for s in f.split("/") if s != ""]
    if not segments:
        return False, "no usable segment"
    if len(segments) > MAX_FOLDER_DEPTH:
        return False, f"deeper than {MAX_FOLDER_DEPTH} levels"
    for s in segments:
        if s in (".", ".."):
            return False, "path traversal"
        if s.split(".")[0].lower() in WINDOWS_RESERVED:
            return False, f"{s!r} is a reserved name on Windows"
        if s.endswith(".") or s.endswith(" "):
            return False, "segment ends with a dot or space"
        if Path(s).suffix.lower() in BUNDLE_SUFFIXES:
            return False, "segment looks like a package bundle"
        if not _segment_ok(s):
            return False, f"disallowed characters in {s!r}"
    return True, "/".join(segments)


def _segment_ok(s: str) -> bool:
    if not s or len(s) > _SEGMENT_MAX:
        return False
    first = unicodedata.category(s[0])
    if not (first[0] in "LN" or s[0] == "_"):
        return False
    for ch in s:
        cat = unicodedata.category(ch)
        if cat[0] in "LM" or cat == "Nd" or ch in _SEGMENT_PUNCT:
            continue
        return False
    return True


def resolve_destination(base: Path, folder: str, filename: str,
                        allowed_roots: tuple[Path, ...]) -> tuple[Path | None, str]:
    """Resolve base/folder/filename and prove it lands inside an allowed root."""
    ok, cleaned = validate_relative_folder(folder)
    if not ok:
        return None, cleaned
    candidate = (base / cleaned / filename)
    # Resolve the *parent*: the file itself does not exist yet.
    try:
        parent = (base / cleaned).resolve()
    except (OSError, RuntimeError) as e:
        return None, f"unresolvable: {e}"
    if not under_any(parent, allowed_roots):
        # This is the real gate. Every allowed root was checked against
        # HARD_DENY_ROOTS at config load, so landing inside one is proof
        # enough; re-testing the deny list here would also reject legitimate
        # roots that live under /private or /var (every macOS temp dir).
        return None, "resolves outside every allowed root"
    if any(is_bundle(p) for p in [parent, *parent.parents]):
        return None, "resolves inside a package bundle"
    return parent / candidate.name, cleaned


def unique_destination(dest: Path) -> Path:
    """Never overwrite. Append ' (2)', ' (3)' ... until the name is free."""
    if not dest.exists():
        return dest
    stem, suffix, parent = dest.stem, dest.suffix, dest.parent
    for n in range(2, 1000):
        cand = parent / f"{stem} ({n}){suffix}"
        if not cand.exists():
            return cand
    raise RuntimeError(f"cannot find a free name for {dest}")
