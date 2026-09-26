"""Turn classifications into a validated, safety-checked move plan.

Every destination is resolved and proven to land inside an allowed root here.
Anything that fails validation is demoted to review, never dropped silently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import extract, safety
from .classify import Decision
from .config import Config
from .dedupe import DuplicateGroup
from .scanner import FileRecord, FolderRecord

DUPLICATES_FOLDER = "_Duplicates"
REVIEW_FOLDER = "_Review"


@dataclass
class PlannedMove:
    record: FileRecord
    dest: Path
    category: str
    folder: str
    confidence: float
    reason: str
    provider: str = ""
    auto: bool = True
    delete: bool = False            # send to Trash instead of moving
    duplicate_of: Path | None = None  # the copy being kept, re-checked at delete

    @property
    def dest_display(self) -> str:
        try:
            return str(self.dest.relative_to(self.record.root))
        except ValueError:
            return str(self.dest)


@dataclass
class Deferred:
    record: FileRecord
    why: str
    suggestion: str = ""
    category: str = ""
    confidence: float = 0.0


@dataclass
class PlannedFolderMove:
    """A whole set, filed as one piece."""
    folder: FolderRecord
    dest: Path              # the folder's new full path
    category: str
    target: str             # its new parent, relative to the root ("" = top)
    confidence: float
    reason: str
    auto: bool = True


@dataclass
class DeferredFolder:
    folder: FolderRecord
    why: str
    suggestion: str = ""    # proposed new full path
    category: str = ""
    confidence: float = 0.0


@dataclass
class Plan:
    auto: list[PlannedMove] = field(default_factory=list)
    review: list[Deferred] = field(default_factory=list)
    duplicates: list[PlannedMove] = field(default_factory=list)
    folder_moves: list[PlannedFolderMove] = field(default_factory=list)
    folder_review: list[DeferredFolder] = field(default_factory=list)
    stayed: int = 0              # files already in a folder that fits
    kept_sets: list[FolderRecord] = field(default_factory=list)
    capped: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def total_moves(self) -> int:
        return len(self.auto) + len(self.duplicates) + len(self.folder_moves)

    @property
    def review_count(self) -> int:
        return len(self.review) + len(self.folder_review)


def _allowed_roots(cfg: Config) -> tuple[Path, ...]:
    return tuple(cfg.scan_roots) + tuple(cfg.media_destinations.values())


def _media_base(cfg: Config, record: FileRecord) -> Path | None:
    """Loose media is routed out to Pictures/Movies/Music (the hybrid rule)."""
    kind = record.extra.get("kind") or extract.kind_of(record.ext)
    if kind in ("image", "video", "audio"):
        return cfg.media_destinations.get(kind)
    return None


def _is_auto(cfg: Config, d: Decision) -> bool:
    b = cfg.behaviour
    if d.confidence >= b.auto_confidence_any:
        return True
    return d.category in cfg.auto_safe_categories and d.confidence >= b.auto_confidence


def _within(path: Path, folder: Path) -> bool:
    return path == folder or folder in path.parents


def _same_folder(a: str, b: str) -> bool:
    """Folder names compare as the filesystem does: case-insensitively on
    macOS and Windows."""
    return a.strip("/").casefold() == b.strip("/").casefold()


def _plan_folders(cfg: Config, plan: Plan, folders: list[FolderRecord],
                  verdicts: dict, wanted: dict) -> dict[Path, Path]:
    """Plan whole-set moves. Returns {old path: new path} for moving sets."""
    allowed = _allowed_roots(cfg)
    sets = [f for f in folders
            if (v := verdicts.get(f.path)) is not None and v.kind == "set"]
    set_paths = [f.path for f in sets]
    moving: dict[Path, Path] = {}

    for f in sets:
        v = verdicts[f.path]
        if v.source in ("placed", "fallback"):
            plan.kept_sets.append(f)
            continue
        target = v.folder.strip()
        if target and not _same_folder(target, ""):
            ok, cleaned = safety.validate_relative_folder(target)
            if not ok:
                plan.folder_review.append(DeferredFolder(
                    f, f"rejected folder {target!r}: {cleaned}",
                    category=v.category, confidence=v.confidence))
                continue
            parent = (f.root / cleaned)
        else:
            cleaned, parent = "", f.root

        current = f.path.parent.relative_to(f.root).as_posix()
        # Answering with the set's own path means "it's fine where it is".
        if _same_folder(cleaned, "" if current == "." else current) or \
                _same_folder(cleaned, f.rel):
            plan.kept_sets.append(f)
            continue
        if _within(parent, f.path):
            plan.folder_review.append(DeferredFolder(
                f, "would move the folder into itself",
                category=v.category, confidence=v.confidence))
            continue
        if any(_within(parent, other) for other in set_paths if other != f.path):
            plan.folder_review.append(DeferredFolder(
                f, "would put it inside another set",
                category=v.category, confidence=v.confidence))
            continue
        try:
            resolved_parent = parent.resolve()
        except OSError:
            resolved_parent = parent
        if not safety.under_any(resolved_parent, allowed):
            plan.folder_review.append(DeferredFolder(
                f, "destination is outside the folders Fily may use",
                category=v.category, confidence=v.confidence))
            continue
        if cleaned and not parent.is_dir() and \
                wanted.get((f.root, cleaned), 0) < cfg.behaviour.min_files_for_new_folder:
            plan.folder_review.append(DeferredFolder(
                f, f"would create {cleaned!r} for only this folder",
                suggestion=str(parent / f.name), category=v.category,
                confidence=v.confidence))
            continue

        dest = safety.unique_destination(parent / f.name)
        # A loose folder at the top is like a loose file: unsorted. One that
        # already sits inside another folder was probably put there, so it
        # only moves on near-certainty.
        needed = (cfg.behaviour.auto_confidence if f.depth == 1
                  else cfg.behaviour.auto_confidence_any)
        move = PlannedFolderMove(folder=f, dest=dest, category=v.category,
                                 target=cleaned, confidence=v.confidence,
                                 reason=v.reason, auto=v.confidence >= needed)
        if move.auto:
            plan.folder_moves.append(move)
            moving[f.path] = dest
        else:
            plan.folder_review.append(DeferredFolder(
                f, f"confidence {v.confidence:.2f} too low to move it by itself",
                suggestion=str(dest), category=v.category, confidence=v.confidence))
    return moving


def build_plan(cfg: Config, records: list[FileRecord],
               decisions: dict[int, Decision],
               duplicate_groups: list[DuplicateGroup],
               folders: list[FolderRecord] | None = None,
               verdicts: dict | None = None) -> Plan:
    plan = Plan()
    allowed = _allowed_roots(cfg)
    folders = folders or []
    verdicts = verdicts or {}
    set_roots = [f.path for f in folders
                 if (v := verdicts.get(f.path)) is not None and v.kind == "set"]
    dumps = {p for p, v in verdicts.items() if getattr(v, "dump", False)}

    # Exact duplicates are handled locally and never rely on the model.
    # Two modes: stage them in _Duplicates/ for a human, or send the spare
    # copies straight to the Trash. Either way the oldest copy is kept, and
    # the trash path re-verifies both files at the moment of deletion.
    trash_dupes = cfg.duplicates_action == "trash"
    dup_ids: set[int] = set()
    for g in duplicate_groups:
        for dup in g.duplicates:
            dup_ids.add(dup.file_id)
            if trash_dupes:
                plan.duplicates.append(PlannedMove(
                    record=dup, dest=dup.path, category="exact-duplicate",
                    folder="(Trash)", confidence=1.0,
                    reason=f"byte-identical to {g.canonical.name}",
                    provider="local", delete=True,
                    duplicate_of=g.canonical.path,
                ))
                continue
            dest, cleaned = safety.resolve_destination(
                dup.root, DUPLICATES_FOLDER, dup.name, allowed)
            if dest is None:
                plan.review.append(Deferred(dup, f"duplicate, but {cleaned}"))
                continue
            plan.duplicates.append(PlannedMove(
                record=dup, dest=dest, category="exact-duplicate",
                folder=DUPLICATES_FOLDER, confidence=1.0,
                reason=f"byte-identical to {g.canonical.name}", provider="local",
            ))

    # How many items want each proposed new folder; used to suppress
    # singletons. Whole sets count as one item each.
    wanted: dict[tuple[Path, str], int] = {}
    for r in records:
        d = decisions.get(r.file_id)
        if not d or r.file_id in dup_ids:
            continue
        ok, cleaned = safety.validate_relative_folder(d.folder)
        if ok:
            base = _media_base(cfg, r) or r.root
            wanted[(base, cleaned)] = wanted.get((base, cleaned), 0) + 1
    for f in folders:
        v = verdicts.get(f.path)
        if v is not None and v.kind == "set" and v.folder:
            ok, cleaned = safety.validate_relative_folder(v.folder)
            if ok:
                wanted[(f.root, cleaned)] = wanted.get((f.root, cleaned), 0) + 1

    moving = _plan_folders(cfg, plan, folders, verdicts, wanted)

    for r in records:
        if r.file_id in dup_ids:
            continue
        d = decisions.get(r.file_id)
        if d is None:
            plan.review.append(Deferred(r, "no classification returned"))
            continue

        nested = r.depth > 0
        ok, cleaned = safety.validate_relative_folder(d.folder)
        # Already in a folder that fits: nothing to do, and nothing to ask.
        # Checked before media routing, so a photo that belongs where it is
        # isn't pulled out to Pictures.
        if nested and ok and _same_folder(cleaned, r.folder_rel):
            plan.stayed += 1
            continue
        if not ok:
            plan.review.append(Deferred(
                r, f"rejected folder {d.folder!r}: {cleaned}",
                category=d.category, confidence=d.confidence))
            continue

        base = _media_base(cfg, r) or r.root
        existing_here = (base / cleaned).is_dir()
        count = wanted.get((base, cleaned), 0)
        # The singleton rule exists to stop the model inventing subject-matter
        # taxonomy one file at a time. It should not block the structural
        # folders (Installers, Screenshots, Archives) that are worth having
        # even for a single file, so auto-safe categories are exempt.
        structural = d.category in cfg.auto_safe_categories
        if (not existing_here and not structural
                and count < cfg.behaviour.min_files_for_new_folder):
            plan.review.append(Deferred(
                r, f"would create {cleaned!r} for only {count} file(s)",
                suggestion=cleaned, category=d.category, confidence=d.confidence))
            continue

        dest, resolved = safety.resolve_destination(base, cleaned, r.name, allowed)
        if dest is None:
            plan.review.append(Deferred(
                r, f"destination rejected: {resolved}",
                category=d.category, confidence=d.confidence))
            continue
        if dest.parent == r.path.parent:
            plan.stayed += 1
            continue
        # A catch-all is sorted out of, never filed into.
        if dest.parent in dumps:
            plan.review.append(Deferred(
                r, f"suggested the catch-all folder {cleaned}/",
                category=d.category, confidence=d.confidence))
            continue
        # Never scatter things into a set's insides; its top is fine.
        if any(_within(dest.parent, s) and dest.parent != s for s in set_roots):
            plan.review.append(Deferred(
                r, "would put it inside a folder that is kept together",
                category=d.category, confidence=d.confidence))
            continue
        # A set moving this run takes its new address with it.
        for old, new in moving.items():
            if _within(dest.parent, old):
                dest = new / dest.relative_to(old)
                break

        # Moving a file *out* of a subfolder it already sits in needs
        # near-certainty; otherwise ask. A loose file uses the usual rules —
        # and so does one in a catch-all folder, which is unsorted by nature.
        settled = nested and r.path.parent not in dumps
        auto = (d.confidence >= cfg.behaviour.auto_confidence_any if settled
                else _is_auto(cfg, d))
        move = PlannedMove(
            record=r, dest=dest, category=d.category, folder=cleaned,
            confidence=d.confidence, reason=d.reason, provider=d.provider,
            auto=auto,
        )
        if move.auto:
            plan.auto.append(move)
        else:
            why = (f"move it out of {r.folder_rel}/? confidence {d.confidence:.2f}"
                   if settled else
                   f"confidence {d.confidence:.2f} below the auto threshold")
            plan.review.append(Deferred(
                r, why, suggestion=str(dest), category=d.category,
                confidence=d.confidence))

    # Blast-radius cap: too many moves in one run means something is wrong.
    if plan.total_moves > cfg.behaviour.max_moves_per_run:
        plan.capped = True
        plan.notes.append(
            f"{plan.total_moves} moves exceeds max_moves_per_run "
            f"({cfg.behaviour.max_moves_per_run}); nothing applied automatically")
        for m in plan.auto + plan.duplicates:
            plan.review.append(Deferred(
                m.record, "held back by the per-run move cap",
                suggestion=str(m.dest), category=m.category,
                confidence=m.confidence))
        for fm in plan.folder_moves:
            plan.folder_review.append(DeferredFolder(
                fm.folder, "held back by the per-run move cap",
                suggestion=str(fm.dest), category=fm.category,
                confidence=fm.confidence))
        plan.auto, plan.duplicates, plan.folder_moves = [], [], []

    return plan
