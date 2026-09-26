"""Apply a decision made during review — from Telegram or the command line.

One place for both, so a file or a whole folder moved by hand gets the same
checks as an automatic move, lands in the undo journal, and is remembered as
placed (so it is never re-sorted afterwards).
"""
from __future__ import annotations

import time
from pathlib import Path

from . import applier, journal, planner, safety, scanner


def suggested_folder(item: dict) -> str:
    """The folder a review item suggests, relative to its root.

    Suggestions are stored as full destination paths; the *whole* relative
    parent is the answer — "Exams/SAT", not just "SAT", which would create a
    new top-level folder instead.
    """
    s = item.get("suggestion") or ""
    if not s:
        return ""
    p = Path(s)
    if p.is_absolute():
        try:
            rel = p.parent.relative_to(Path(item["root"])).as_posix()
            return "" if rel == "." else rel
        except ValueError:
            return p.parent.name
    return s


def _remember(cfg, jr: journal.Journal) -> None:
    cache = journal.Cache(cfg)
    try:
        cache.record_journal(jr.path)
    finally:
        cache.close()


def _allowed(cfg) -> tuple[Path, ...]:
    return tuple(cfg.scan_roots) + tuple(cfg.media_destinations.values())


def move_reviewed(cfg, item: dict, folder: str, provider: str = "manual",
                  jr: journal.Journal | None = None) -> tuple[bool, str]:
    """Move a reviewed file, or a whole folder, into `folder` (relative to
    its root). Pass `jr` to collect a whole review session in one undoable
    journal. Returns (ok, message)."""
    if item.get("type") == "folder":
        return _move_folder(cfg, item, folder, provider, jr)
    src, root = Path(item["path"]), Path(item["root"])
    if not src.exists():
        return False, "that file is no longer there"
    dest, why = safety.resolve_destination(root, folder, src.name, _allowed(cfg))
    if dest is None:
        return False, f"can't use that folder — {why}"
    try:
        st = src.stat()
    except OSError as e:
        return False, f"unreadable: {e.strerror}"
    record = scanner.FileRecord(
        path=src, root=root, size=st.st_size, mtime=st.st_mtime,
        ctime=getattr(st, "st_birthtime", st.st_ctime),
        ext=src.suffix.lower().lstrip("."))
    move = planner.PlannedMove(
        record=record, dest=dest, category=item.get("category", "manual"),
        folder=folder, confidence=1.0, reason="chosen in review",
        provider=provider)
    jr = jr or journal.Journal(cfg, journal.new_run_id())
    result = applier.apply_moves(cfg, [move], jr)
    if result.errors:
        return False, result.errors[0][1]
    _remember(cfg, jr)
    return True, folder


def _move_folder(cfg, item: dict, folder: str, provider: str,
                 jr: journal.Journal | None = None) -> tuple[bool, str]:
    src, root = Path(item["path"]), Path(item["root"])
    if not src.is_dir():
        return False, "that folder is no longer there"
    ok, cleaned = safety.validate_relative_folder(folder)
    if not ok:
        return False, f"can't use that folder — {cleaned}"
    parent = root / cleaned
    if parent == src or src in parent.parents:
        return False, "can't move a folder into itself"
    try:
        resolved = parent.resolve()
    except OSError:
        resolved = parent
    if not safety.under_any(resolved, _allowed(cfg)):
        return False, "that is outside the folders Fily may use"
    fm = planner.PlannedFolderMove(
        folder=scanner.FolderRecord(path=src, root=root),
        dest=safety.unique_destination(parent / src.name),
        category=item.get("category", "manual"), target=cleaned,
        confidence=1.0, reason="chosen in review")
    jr = jr or journal.Journal(cfg, journal.new_run_id())
    # You chose this, so a recent edit inside doesn't block it; an open
    # file still does.
    result = applier.apply_folder_moves(cfg, [fm], jr, respect_quarantine=False)
    if result.errors:
        return False, result.errors[0][1]
    _remember(cfg, jr)
    return True, cleaned


def trash_reviewed(cfg, item: dict, provider: str = "manual",
                   jr: journal.Journal | None = None) -> tuple[bool, str]:
    """Send a reviewed file to the Trash / Recycle Bin. Never a folder."""
    from . import trash
    if item.get("type") == "folder":
        return False, "whole folders can't be deleted from review"
    try:
        done = trash.send_to_trash(Path(item["path"]))
    except trash.TrashError as e:
        return False, str(e)
    jr = jr or journal.Journal(cfg, journal.new_run_id())
    jr.record(journal.MoveEntry(
        src=str(done.original), dst=str(done.trashed_to or ""),
        sha256=done.sha, size=done.size, ts=time.time(), created_dirs=[],
        hash_method=done.method, action="trash",
        category=item.get("category", "manual"), confidence=1.0,
        provider=provider))
    _remember(cfg, jr)
    return True, "moved to the Trash"
