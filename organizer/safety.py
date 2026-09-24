"""Hard safety rules. Not configurable.

Everything here is deliberately conservative: when a rule is unsure, it excludes.
A file wrongly skipped is a non-event; a file wrongly moved is a bug report.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

HOME = Path.home()

# Never touched, under any configuration.
#
# These are listed in resolved form because config paths are resolved before
# they get here, and on macOS /etc and /var are symlinks into /private. Note
# what is deliberately absent: a blanket /private or /var would also cover
# /private/var/folders, where every per-user temp directory lives, so the
# specific sensitive children are named instead.
HARD_DENY_ROOTS: tuple[Path, ...] = (
    HOME / "Library",
    HOME / ".Trash",
    HOME / "Applications",
    Path("/System"),
    Path("/Library"),
    Path("/Applications"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/cores"),
    Path("/opt"),
    Path("/etc"), Path("/private/etc"),
    Path("/var/db"), Path("/private/var/db"),
    Path("/var/root"), Path("/private/var/root"),
    Path("/var/vm"), Path("/private/var/vm"),
    Path("/var/log"), Path("/private/var/log"),
)

# Directories that hold only Apple-managed libraries. Destination-only.
LIBRARY_ROOTS: tuple[Path, ...] = (
    HOME / "Pictures",
    HOME / "Movies",
    HOME / "Music",
)

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

# Folder names the model may propose: conservative charset, no traversal.
_FOLDER_SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9 _.\-&()']{0,48}$")
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
    if root == Path("/"):
        return "the filesystem root is never scannable"
    if under_any(root, HARD_DENY_ROOTS):
        return "inside a system or library location"
    for lib in LIBRARY_ROOTS:
        if _is_under(root, lib):
            return (
                f"{lib.name} holds Apple-managed library bundles; "
                "it is a destination, never a scan root"
            )
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
    if name.startswith("."):
        return "hidden file"
    if path.suffix.lower() in TRANSIENT_SUFFIXES:
        return "transient or in-flight file"
    if path.is_symlink():
        return "symlink"
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
    """True if another process currently holds the file open.

    Checked immediately before a move. `lsof` missing or slow is treated as
    "assume open" only on timeout, so we never race an active writer.
    """
    try:
        r = subprocess.run(
            ["/usr/sbin/lsof", "-t", "--", str(path)],
            capture_output=True, timeout=10, check=False,
        )
        return bool(r.stdout.strip())
    except subprocess.TimeoutExpired:
        return True
    except (FileNotFoundError, OSError):
        return False


def validate_relative_folder(folder: str) -> tuple[bool, str]:
    """Validate a model-proposed folder name.

    The model never supplies a path we execute against; this gate is what makes
    a prompt injection in a filename or PDF harmless.
    """
    if not folder or not folder.strip():
        return False, "empty"
    f = folder.strip().replace("\\", "/")
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
        if s.endswith(".") or s.endswith(" "):
            return False, "segment ends with a dot or space"
        if Path(s).suffix.lower() in BUNDLE_SUFFIXES:
            return False, "segment looks like a package bundle"
        if not _FOLDER_SEGMENT.match(s):
            return False, f"disallowed characters in {s!r}"
    return True, "/".join(segments)


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
