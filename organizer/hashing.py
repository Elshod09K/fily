"""Content hashing, shared by dedupe, the decision cache and undo verification."""
from __future__ import annotations

import hashlib
from pathlib import Path

CHUNK = 1024 * 1024


def sha256_file(path: Path, chunk: int = CHUNK) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            b = fh.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def quick_signature(path: Path, size: int, head: int = 65536) -> str:
    """Cheap pre-filter: size + first and last 64 KiB.

    Lets us avoid full-hashing a 370 MB .dmg unless something else shares its
    exact size.
    """
    h = hashlib.sha256()
    h.update(str(size).encode())
    with path.open("rb") as fh:
        h.update(fh.read(head))
        if size > head * 2:
            fh.seek(-head, 2)
            h.update(fh.read(head))
    return h.hexdigest()


# Files above this are identified by size + head/tail rather than a full read:
# hashing a 370 MB installer on every run costs more than it is worth.
FULL_HASH_LIMIT = 256 * 1024 * 1024


def content_id(path: Path, size: int | None = None,
               limit: int = FULL_HASH_LIMIT) -> tuple[str, str]:
    """Return (value, method) identifying this file's contents.

    Both the journal and undo call this, which is the point: an earlier
    version hashed large files one way when recording a move and another way
    when verifying it, so anything over the limit could never be restored.
    """
    size = path.stat().st_size if size is None else size
    if size > limit:
        return quick_signature(path, size), "quick"
    return sha256_file(path), "sha256"


def verify_content(path: Path, expected: str, method: str) -> bool:
    """Re-derive a file's id with the method that recorded it."""
    try:
        size = path.stat().st_size
        if method == "quick":
            return quick_signature(path, size) == expected
        return sha256_file(path) == expected
    except OSError:
        return False
