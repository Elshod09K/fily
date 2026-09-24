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
from .scanner import FileRecord

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
class Plan:
    auto: list[PlannedMove] = field(default_factory=list)
    review: list[Deferred] = field(default_factory=list)
    duplicates: list[PlannedMove] = field(default_factory=list)
    capped: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def total_moves(self) -> int:
        return len(self.auto) + len(self.duplicates)


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


def build_plan(cfg: Config, records: list[FileRecord],
               decisions: dict[int, Decision],
               duplicate_groups: list[DuplicateGroup]) -> Plan:
    plan = Plan()
    allowed = _allowed_roots(cfg)

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

    # How many files want each proposed new folder; used to suppress singletons.
    wanted: dict[tuple[Path, str], int] = {}
    for r in records:
        d = decisions.get(r.file_id)
        if not d or r.file_id in dup_ids:
            continue
        ok, cleaned = safety.validate_relative_folder(d.folder)
        if ok:
            base = _media_base(cfg, r) or r.root
            wanted[(base, cleaned)] = wanted.get((base, cleaned), 0) + 1

    for r in records:
        if r.file_id in dup_ids:
            continue
        d = decisions.get(r.file_id)
        if d is None:
            plan.review.append(Deferred(r, "no classification returned"))
            continue

        ok, cleaned = safety.validate_relative_folder(d.folder)
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
            plan.review.append(Deferred(
                r, "already in the right place", category=d.category,
                confidence=d.confidence))
            continue

        move = PlannedMove(
            record=r, dest=dest, category=d.category, folder=cleaned,
            confidence=d.confidence, reason=d.reason, provider=d.provider,
            auto=_is_auto(cfg, d),
        )
        if move.auto:
            plan.auto.append(move)
        else:
            plan.review.append(Deferred(
                r, f"confidence {d.confidence:.2f} below the auto threshold",
                suggestion=str(dest), category=d.category, confidence=d.confidence))

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
        plan.auto, plan.duplicates = [], []

    return plan
