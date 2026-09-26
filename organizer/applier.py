"""Execute a move plan and reverse one.

Invariants:
  * A move is journaled *before* the rename is considered complete.
  * Nothing is ever overwritten; collisions get a numbered suffix.
  * Nothing is ever deleted.
  * Cross-volume moves fall back to copy+verify+unlink, never a bare copy.
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import host, safety, trash
from .config import Config
from .hashing import content_id, sha256_file, verify_content
from .journal import Journal, MoveEntry, read_journal


@dataclass
class MoveResult:
    moved: int = 0
    folders_moved: int = 0
    trashed: int = 0
    trashed_bytes: int = 0
    failed: int = 0
    errors: list[tuple[str, str]] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def _ensure_dirs(dest_parent: Path) -> list[str]:
    """Create the destination folder, returning dirs we actually created."""
    created: list[str] = []
    missing: list[Path] = []
    p = dest_parent
    while not p.exists():
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(parents=False, exist_ok=True)
        created.append(str(d))
    return created


def _move_file(src: Path, dst: Path) -> None:
    """Rename when possible; otherwise copy, verify, then unlink the source."""
    try:
        os.rename(src, dst)
        return
    except OSError:
        pass  # cross-device, or a rename the kernel refused
    src_hash = sha256_file(src)
    shutil.copy2(src, dst)
    if sha256_file(dst) != src_hash:
        dst.unlink(missing_ok=True)
        raise OSError(f"copy verification failed for {src}")
    src.unlink()


def apply_moves(cfg: Config, actions, journal: Journal,
                dry_run: bool = False) -> MoveResult:
    """`actions` is an iterable of PlannedMove (see planner.py)."""
    result = MoveResult()
    for a in actions:
        src = a.record.path
        try:
            if getattr(a, "delete", False):
                ok, why = _trash_one(cfg, a, journal, dry_run)
                if ok:
                    result.trashed += 1
                    result.trashed_bytes += a.record.size
                else:
                    result.errors.append((str(src), why))
                    result.failed += 1
                continue
            if not src.exists():
                result.errors.append((str(src), "vanished before the move"))
                result.failed += 1
                continue
            if safety.is_file_open(src):
                result.errors.append((str(src), "open in another process"))
                result.failed += 1
                continue

            dest = safety.unique_destination(a.dest)
            if dry_run:
                result.moved += 1
                continue

            size = src.stat().st_size
            sha, method = content_id(src, size)
            created = _ensure_dirs(dest.parent)

            # Journal first: an interruption mid-rename is still reversible.
            journal.record(MoveEntry(
                src=str(src), dst=str(dest), sha256=sha, size=size, ts=time.time(),
                created_dirs=created, hash_method=method, category=a.category,
                confidence=a.confidence, provider=a.provider,
            ))
            _move_file(src, dest)
            result.moved += 1
        except Exception as e:  # a single bad file must not abort the run
            result.errors.append((str(src), f"{type(e).__name__}: {e}"))
            result.failed += 1
    return result


def _trash_one(cfg, action, journal, dry_run: bool) -> tuple[bool, str]:
    """Send one file to the Trash, re-verifying first when it is a duplicate."""
    src = action.record.path
    if not src.exists():
        return False, "vanished before deletion"
    if safety.is_file_open(src):
        return False, "open in another process"

    keeper = getattr(action, "duplicate_of", None)
    if keeper is not None:
        sha = action.record.sha256
        method = "quick" if (sha or "").startswith("size:") else "sha256"
        if sha and sha.startswith("size:"):
            sha = sha.split(":", 2)[2]
        ok, why = trash.safe_to_trash_duplicate(src, keeper, sha, method)
        if not ok:
            return False, why

    if dry_run:
        return True, "would trash"

    try:
        done = trash.send_to_trash(src)
    except trash.TrashError as e:
        return False, str(e)

    journal.record(MoveEntry(
        src=str(done.original), dst=str(done.trashed_to or ""),
        sha256=done.sha, size=done.size, ts=time.time(), created_dirs=[],
        hash_method=done.method, action="trash",
        category=action.category, confidence=action.confidence,
        provider=action.provider,
    ))
    return True, "trashed"


def folder_manifest(path: Path) -> tuple[str, int, int, float]:
    """(digest, file count, total size, newest change) over a folder's
    visible files. Identifies its contents without reading them."""
    import hashlib
    entries = []
    for dirpath, _, filenames in os.walk(path):
        for fn in filenames:
            if safety.is_ignored_name(fn) or fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            try:
                st = p.lstat()
            except OSError:
                continue
            entries.append((p.relative_to(path).as_posix(), st.st_size, st.st_mtime))
    h = hashlib.sha1()
    for rel, size, mtime in sorted(entries):
        h.update(f"{rel}\0{size}\0{int(mtime)}\n".encode("utf-8"))
    return (h.hexdigest(), len(entries), sum(e[1] for e in entries),
            max((e[2] for e in entries), default=0.0))


def apply_folder_moves(cfg, moves, journal, dry_run: bool = False,
                       respect_quarantine: bool = True) -> MoveResult:
    """Move whole sets. Same guarantees as file moves: nothing busy, nothing
    recently changed, journaled before the rename, never overwriting."""
    result = MoveResult()
    quarantine = cfg.behaviour.quarantine_hours * 3600
    for m in moves:
        src = m.folder.path
        try:
            if not src.is_dir() or src.is_symlink():
                result.errors.append((str(src), "folder is no longer there"))
                result.failed += 1
                continue
            digest, count, size, newest = folder_manifest(src)
            if respect_quarantine and newest and time.time() - newest < quarantine:
                result.errors.append((str(src), "something inside changed recently"))
                result.failed += 1
                continue
            if host.folder_in_use(src):
                result.errors.append((str(src), "a file inside is open"))
                result.failed += 1
                continue
            dest = safety.unique_destination(m.dest)
            if dest == src or src in dest.parents:
                result.errors.append((str(src), "would move the folder into itself"))
                result.failed += 1
                continue
            if dry_run:
                result.folders_moved += 1
                continue
            created = _ensure_dirs(dest.parent)
            journal.record(MoveEntry(
                src=str(src), dst=str(dest), sha256=digest, size=size,
                ts=time.time(), created_dirs=created, hash_method="manifest",
                action="move_dir", category=m.category, confidence=m.confidence,
                provider="ai"))
            os.rename(src, dest)            # same volume: atomic, never a copy
            result.folders_moved += 1
        except OSError as e:
            result.errors.append((str(src), f"{type(e).__name__}: {e.strerror or e}"))
            result.failed += 1
    return result


@dataclass
class UndoResult:
    restored: int = 0
    untrashed: int = 0
    skipped: int = 0
    dirs_removed: int = 0
    problems: list[tuple[str, str]] = None

    def __post_init__(self):
        if self.problems is None:
            self.problems = []


def undo_run(cfg: Config, journal_path: Path, dry_run: bool = False) -> UndoResult:
    """Reverse a run. Verifies content hash before restoring anything."""
    res = UndoResult()
    entries = read_journal(journal_path)
    created_dirs: set[str] = set()

    for e in reversed(entries):
        src, dst = Path(e.src), Path(e.dst)
        created_dirs.update(e.created_dirs or [])

        if (e.action or "move") == "move_dir":
            if not dst.is_dir():
                res.problems.append((str(dst), "folder is no longer where it was moved"))
                res.skipped += 1
                continue
            if src.exists():
                res.problems.append((str(src), "something is back at the original path"))
                res.skipped += 1
                continue
            if folder_manifest(dst)[0] != e.sha256:
                res.problems.append((str(dst), "folder's contents changed since the move; "
                                               "not restored"))
                res.skipped += 1
                continue
            if dry_run:
                res.restored += 1
                continue
            try:
                src.parent.mkdir(parents=True, exist_ok=True)
                os.rename(dst, src)
                res.restored += 1
            except OSError as err:
                res.problems.append((str(dst), f"{type(err).__name__}: {err}"))
                res.skipped += 1
            continue

        if (e.action or "move") == "trash":
            if dry_run:
                res.restored += 1
                res.untrashed += 1
                continue
            ok, why = trash.restore(e)
            if ok:
                res.restored += 1
                res.untrashed += 1
            else:
                res.problems.append((str(src), why))
                res.skipped += 1
            continue

        if not dst.exists():
            res.problems.append((str(dst), "no longer at the destination; left alone"))
            res.skipped += 1
            continue
        if src.exists():
            res.problems.append((str(src), "something is back at the original path"))
            res.skipped += 1
            continue
        if not verify_content(dst, e.sha256, e.hash_method or "sha256"):
            res.problems.append(
                (str(dst), "content changed since the move; not restored"))
            res.skipped += 1
            continue
        if dry_run:
            res.restored += 1
            continue
        try:
            src.parent.mkdir(parents=True, exist_ok=True)
            _move_file(dst, src)
            res.restored += 1
        except Exception as err:
            res.problems.append((str(dst), f"{type(err).__name__}: {err}"))
            res.skipped += 1

    if not dry_run:
        for d in sorted(created_dirs, key=len, reverse=True):
            p = Path(d)
            try:
                if p.is_dir() and not any(
                    x for x in p.iterdir() if x.name not in safety.IGNORED_NAMES
                ):
                    for junk in p.iterdir():
                        junk.unlink(missing_ok=True)   # only .DS_Store et al
                    p.rmdir()
                    res.dirs_removed += 1
            except OSError:
                pass
    return res
