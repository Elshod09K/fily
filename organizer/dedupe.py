"""Exact-duplicate detection. Entirely local: costs nothing, sends nothing.

Cheap first (group by size), then a head/tail signature, and only then a full
sha256 — so a 370 MB installer is never fully hashed unless another file shares
its exact byte count.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .hashing import quick_signature, sha256_file
from .scanner import FileRecord


@dataclass
class DuplicateGroup:
    sha256: str
    canonical: FileRecord
    duplicates: list[FileRecord] = field(default_factory=list)

    @property
    def wasted_bytes(self) -> int:
        return sum(d.size for d in self.duplicates)


def _canonical_of(records: list[FileRecord]) -> FileRecord:
    """Keep the oldest copy; break ties on the shortest, plainest name.

    The original download almost always predates its ' (1)' sibling, and the
    shorter name is almost always the un-suffixed one.
    """
    return sorted(records, key=lambda r: (r.mtime, len(r.name), r.name))[0]


def find_duplicates(records: list[FileRecord]) -> tuple[list[DuplicateGroup], dict[int, str]]:
    """Return exact-duplicate groups, plus sha256 for every hashed file."""
    hashes: dict[int, str] = {}

    by_size: dict[int, list[FileRecord]] = defaultdict(list)
    for r in records:
        by_size[r.size].append(r)

    candidates: list[FileRecord] = []
    for size, group in by_size.items():
        if len(group) > 1:
            candidates.extend(group)

    by_sig: dict[str, list[FileRecord]] = defaultdict(list)
    for r in candidates:
        try:
            by_sig[quick_signature(r.path, r.size)].append(r)
        except OSError:
            continue

    groups: list[DuplicateGroup] = []
    for sig, group in by_sig.items():
        if len(group) < 2:
            continue
        by_hash: dict[str, list[FileRecord]] = defaultdict(list)
        for r in group:
            try:
                h = sha256_file(r.path)
            except OSError:
                continue
            r.sha256 = h
            hashes[r.file_id] = h
            by_hash[h].append(r)
        for h, same in by_hash.items():
            if len(same) < 2:
                continue
            canonical = _canonical_of(same)
            groups.append(DuplicateGroup(
                sha256=h, canonical=canonical,
                duplicates=[r for r in same if r is not canonical],
            ))

    groups.sort(key=lambda g: g.wasted_bytes, reverse=True)
    return groups, hashes


def ensure_hashes(records: list[FileRecord], max_bytes: int = 256 * 1024 * 1024) -> None:
    """Fill in sha256 for cache lookups, skipping very large unhashed files."""
    for r in records:
        if r.sha256:
            continue
        if r.size > max_bytes:
            # Stable stand-in so the cache still works for huge files.
            r.sha256 = f"size:{r.size}:{quick_signature(r.path, r.size)}"
            continue
        try:
            r.sha256 = sha256_file(r.path)
        except OSError:
            r.sha256 = None
