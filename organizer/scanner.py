"""Walk the scan roots and emit eligible file records.

The walk is the only place that decides what is *visible* to the rest of the
pipeline. If something must never be touched, it should be excluded here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from . import safety
from .config import Config


@dataclass
class FileRecord:
    path: Path
    root: Path
    size: int
    mtime: float
    ctime: float
    ext: str
    sha256: str | None = None
    snippet: str = ""
    snippet_note: str = ""
    file_id: int = -1
    extra: dict = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def rel(self) -> str:
        try:
            return str(self.path.relative_to(self.root))
        except ValueError:
            return self.path.name


@dataclass
class ScanResult:
    files: list[FileRecord]
    skipped: list[tuple[Path, str]]
    pruned_dirs: list[tuple[Path, str]]
    existing_folders: dict[Path, list[str]]
    unreadable_roots: list[tuple[Path, str]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "files": len(self.files),
            "skipped": len(self.skipped),
            "pruned_dirs": len(self.pruned_dirs),
        }


def _existing_subfolders(root: Path, limit: int = 60) -> list[str]:
    """Folders already present under a root, so the model can prefer them."""
    out: list[str] = []
    for dirpath, dirnames, _ in os.walk(root):
        d = Path(dirpath)
        depth = len(d.relative_to(root).parts)
        if depth >= safety.MAX_FOLDER_DEPTH:
            dirnames[:] = []

        keep: list[str] = []
        for n in sorted(dirnames):
            child = d / n
            # A project directory is not a destination, and neither is
            # anything inside it. Dropping it from dirnames stops the walk
            # descending, which is what previously let a skill's own
            # references/ folder be offered as somewhere to file documents.
            if (n.startswith(".") or safety.is_bundle(child)
                    or n in safety.PROJECT_MARKERS
                    or safety.project_marker(child)):
                continue
            keep.append(n)
            out.append(str(child.relative_to(root)))
            if len(out) >= limit:
                dirnames[:] = []
                return sorted(out)
        dirnames[:] = keep
    return sorted(out)


def root_readable(root: Path) -> tuple[bool, str]:
    """Can this process actually list `root`?

    Checked explicitly because os.walk swallows a permission error and yields
    nothing, so a folder macOS refuses to show us looks exactly like an empty
    one. Under launchd that refusal is the normal state until the interpreter
    is granted Full Disk Access — the single most likely failure on a fresh
    install, and without this check it would report "nothing to organise"
    every night, forever.
    """
    try:
        with os.scandir(root) as it:
            next(it, None)
        return True, ""
    except PermissionError:
        return False, "permission denied (macOS privacy protection)"
    except OSError as e:
        return False, e.strerror or type(e).__name__


def scan(cfg: Config, roots: tuple[Path, ...] | None = None) -> ScanResult:
    roots = roots or cfg.scan_roots
    files: list[FileRecord] = []
    skipped: list[tuple[Path, str]] = []
    pruned: list[tuple[Path, str]] = []
    existing: dict[Path, list[str]] = {}
    unreadable: list[tuple[Path, str]] = []

    deny = tuple(cfg.deny_paths) + safety.HARD_DENY_ROOTS + safety.LIBRARY_ROOTS

    for root in roots:
        if not root.is_dir():
            continue
        ok, why = root_readable(root)
        if not ok:
            unreadable.append((root, why))
            continue
        existing[root] = _existing_subfolders(root)

        max_depth = max(0, cfg.behaviour.scan_depth - 1)

        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            here = Path(dirpath)
            depth = len(here.relative_to(root).parts)

            # Beyond scan_depth a directory's contents are already organised:
            # list it as a destination, never harvest files out of it.
            if depth >= max_depth:
                for d in dirnames:
                    pruned.append((here / d, "below scan_depth; left intact"))
                dirnames[:] = []

            if here != root:
                if safety.under_any(here, deny):
                    dirnames[:] = []
                    pruned.append((here, "inside a denied path"))
                    continue
                marker = safety.project_marker(here)
                if marker:
                    dirnames[:] = []
                    pruned.append((here, f"code project (found {marker})"))
                    continue

            keep: list[str] = []
            for d in dirnames:
                child = here / d
                if d.startswith("."):
                    pruned.append((child, "hidden directory"))
                elif safety.is_bundle(child):
                    pruned.append((child, f"macOS package bundle ({child.suffix})"))
                elif child.is_symlink():
                    pruned.append((child, "symlinked directory"))
                elif safety.under_any(child, deny):
                    pruned.append((child, "inside a denied path"))
                elif safety.project_marker(child):
                    pruned.append((child, "code project"))
                else:
                    keep.append(d)
            dirnames[:] = keep

            for fn in filenames:
                p = here / fn
                if safety.is_ignored_name(fn):
                    continue  # never counted, never reported
                try:
                    st = p.lstat()
                except OSError as e:
                    skipped.append((p, f"stat failed: {e.strerror}"))
                    continue
                if safety.is_bundle(p) and p.is_dir():
                    continue
                reason = safety.skip_reason(p, st, cfg.behaviour.quarantine_hours)
                if reason:
                    skipped.append((p, reason))
                    continue
                files.append(
                    FileRecord(
                        path=p, root=root, size=st.st_size,
                        mtime=st.st_mtime,
                        ctime=getattr(st, "st_birthtime", st.st_ctime),
                        ext=p.suffix.lower().lstrip("."),
                    )
                )

    files.sort(key=lambda f: (str(f.root), f.rel.lower()))
    for i, f in enumerate(files):
        f.file_id = i
    return ScanResult(files=files, skipped=skipped, pruned_dirs=pruned,
                      existing_folders=existing, unreadable_roots=unreadable)
