"""Deleting, done carefully.

Nothing here calls unlink. Files go to the macOS Trash, which means:
  * Finder's "Put Back" still works,
  * the user decides when space is actually reclaimed,
  * and a wrong call costs a few seconds, not a file.

Where the file lands in ~/.Trash is recorded so `organize undo` can restore it
without Finder, because the Trash renames on collision and guessing is not
good enough.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import host
from .hashing import content_id, verify_content

# macOS: ~/.Trash. Windows has no single readable folder for the Recycle
# Bin, so there is nothing to read back and recovery goes through Explorer.
TRASH: Path | None = host.TRASH_DIR


class TrashError(RuntimeError):
    pass


def trash_readable() -> bool:
    """Can this process list ~/.Trash?

    macOS protects the Trash under TCC, so a process without Full Disk Access
    can *put* things there (via the Foundation API send2trash uses) but cannot
    read the directory back. That asymmetry decides whether `organize undo`
    can un-delete, or whether recovery has to go through Finder's Put Back.
    """
    if TRASH is None:
        return False
    try:
        next(iter(os.scandir(TRASH)), None)
        return True
    except OSError:
        return False


@dataclass
class Trashed:
    original: Path
    trashed_to: Path | None
    sha: str
    method: str
    size: int


def _snapshot() -> set[str]:
    try:
        return {p.name for p in TRASH.iterdir()} if TRASH else set()
    except OSError:
        return set()


def _locate(before: set[str], name: str, sha: str, method: str) -> Path | None:
    """Find where the Trash actually put the file.

    It renames on collision ("report.pdf" -> "report 2.pdf"), so match on
    content rather than trusting the name.
    """
    if TRASH is None:
        return None
    try:
        candidates = [p for p in TRASH.iterdir() if p.name not in before]
    except OSError:
        return None
    exact = [p for p in candidates if p.name == name]
    for p in exact + candidates:
        try:
            if p.is_file() and verify_content(p, sha, method):
                return p
        except OSError:
            continue
    return None


def send_to_trash(path: Path) -> Trashed:
    """Move one file to the Trash, recording where it ended up."""
    from send2trash import send2trash

    if not path.exists():
        raise TrashError("file is already gone")
    if path.is_dir():
        raise TrashError("refusing to trash a directory")
    if path.is_symlink():
        raise TrashError("refusing to trash a symlink")

    size = path.stat().st_size
    sha, method = content_id(path, size)
    before = _snapshot()
    try:
        send2trash(str(path))
    except Exception as e:
        raise TrashError(f"{type(e).__name__}: {e}") from e
    if path.exists():
        raise TrashError("the file is still there after trashing")
    return Trashed(original=path, trashed_to=_locate(before, path.name, sha, method),
                   sha=sha, method=method, size=size)


def restore(entry) -> tuple[bool, str]:
    """Put a trashed file back where it came from.

    Only possible when this process can read ~/.Trash. Without Full Disk
    Access it cannot, and the honest answer is to point at Finder rather than
    fail obscurely — Put Back restores to the original location anyway.
    """
    original = Path(entry.src)
    if original.exists():
        return False, "something is already back at the original path"
    if not trash_readable():
        return False, (f"it is in the {host.TRASH_NAME} — restore it with "
                       f"{host.RESTORE_HINT}")

    candidate = Path(entry.dst) if entry.dst else None
    if candidate is None or not candidate.exists():
        # The recorded path is gone; try to find it by content anyway.
        try:
            pool = [p for p in TRASH.iterdir() if p.is_file()] if TRASH else []
        except OSError:
            pool = []
        candidate = next(
            (p for p in pool
             if verify_content(p, entry.sha256, entry.hash_method or "sha256")),
            None)
    if candidate is None:
        return False, "no longer in the Trash (emptied?) — cannot restore"

    if not verify_content(candidate, entry.sha256, entry.hash_method or "sha256"):
        return False, "the trashed copy no longer matches; left alone"
    try:
        original.parent.mkdir(parents=True, exist_ok=True)
        os.rename(candidate, original)
    except OSError as e:
        return False, f"could not restore: {e.strerror}"
    return True, "restored"


def safe_to_trash_duplicate(duplicate: Path, canonical: Path,
                            sha: str, method: str) -> tuple[bool, str]:
    """Re-check, at the moment of deletion, that this really is a spare copy.

    The scan that found the duplicate may be minutes old. Deleting is the one
    irreversible-feeling action here, so nothing is taken on trust: the keeper
    must still exist and both files must still hash the same.
    """
    if not duplicate.exists():
        return False, "already gone"
    if not canonical.exists():
        return False, "the copy being kept has disappeared — keeping this one"
    try:
        if duplicate.samefile(canonical):
            return False, "this is the same file, not a copy"
    except OSError:
        return False, "could not compare with the kept copy"
    if not verify_content(canonical, sha, method):
        return False, "the kept copy changed since the scan"
    if not verify_content(duplicate, sha, method):
        return False, "this copy changed since the scan"
    return True, "verified identical to the kept copy"
