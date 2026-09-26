"""Human-readable run report."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from . import host
from .config import Config
from .planner import Plan
from .scanner import ScanResult


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}"
        n /= 1024.0
    return f"{n:.0f} TB"


def write_report(cfg: Config, run_id: str, scan: ScanResult, plan: Plan,
                 applied, dry_run: bool, attempts=None,
                 duplicate_groups=None) -> Path:
    out = cfg.state_dir / "runs" / f"{run_id}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    L: list[str] = []
    mode = "DRY RUN — nothing was moved" if dry_run else "applied"

    L.append(f"# Organizer run {run_id}")
    L.append("")
    L.append(f"*{datetime.now():%Y-%m-%d %H:%M}* · {mode}")
    L.append("")
    L.append("| | |")
    L.append("|---|---|")
    L.append(f"| Scanned | {len(scan.files)} files"
             + (f" in {len(scan.folders)} subfolders" if scan.folders else "") + " |")
    L.append(f"| Moved | {applied.moved if applied else 0} |")
    if applied and applied.folders_moved:
        L.append(f"| Folders moved as a whole | {applied.folders_moved} |")
    if plan.stayed:
        L.append(f"| Already in place | {plan.stayed} |")
    if plan.kept_sets or plan.folder_moves:
        L.append(f"| Folders kept together | {len(plan.kept_sets) + len(plan.folder_moves)} |")
    if applied and applied.trashed:
        L.append(f"| Deleted to Trash | {applied.trashed} "
                 f"({_size(applied.trashed_bytes)}) |")
    L.append(f"| Queued for review | {plan.review_count} |")
    L.append(f"| Skipped | {len(scan.skipped)} |")
    if applied and applied.failed:
        L.append(f"| Failed | {applied.failed} |")
    L.append("")

    for note in plan.notes:
        L.append(f"> **Note:** {note}")
    if plan.notes:
        L.append("")

    if plan.auto:
        L.append(f"## Moved ({len(plan.auto)})")
        L.append("")
        by_folder: dict[str, list] = {}
        for m in plan.auto:
            by_folder.setdefault(m.folder, []).append(m)
        for folder in sorted(by_folder):
            L.append(f"**{folder}/**")
            L.append("")
            for m in sorted(by_folder[folder], key=lambda x: x.record.name.lower()):
                L.append(f"- `{m.record.name}` — {m.reason} "
                         f"*(conf {m.confidence:.2f})*")
            L.append("")

    if plan.folder_moves:
        L.append(f"## Folders moved as a whole ({len(plan.folder_moves)})")
        L.append("")
        L.append("Each of these was judged a set whose files belong together, so "
                 "it moved as one piece; nothing inside was split up.")
        L.append("")
        for fm in plan.folder_moves:
            L.append(f"- `{fm.folder.rel}/` ({fm.folder.file_count} files) → "
                     f"`{fm.target or '(top level)'}/` — {fm.reason} "
                     f"*(conf {fm.confidence:.2f})*")
        L.append("")

    if plan.kept_sets:
        L.append(f"<details><summary>Kept together where they are "
                 f"({len(plan.kept_sets)})</summary>")
        L.append("")
        for f in plan.kept_sets:
            L.append(f"- `{f.rel}/` ({f.file_count} files)")
        L.append("")
        L.append("</details>")
        L.append("")

    if plan.duplicates:
        total = sum(m.record.size for m in plan.duplicates)
        deleted = any(m.delete for m in plan.duplicates)
        L.append(f"## Exact duplicates ({len(plan.duplicates)}, {_size(total)})")
        L.append("")
        if deleted:
            L.append(f"Sent to the **{host.TRASH_NAME}**. The oldest copy of "
                     "each was kept, and both files were re-checked as "
                     "byte-identical immediately before deleting. To get one "
                     f"back: {host.RESTORE_HINT}.")
        else:
            L.append("Moved to `_Duplicates/`. Nothing was deleted — review and "
                     "remove them yourself, or run `organize prune`.")
        L.append("")
        for m in sorted(plan.duplicates, key=lambda x: -x.record.size):
            L.append(f"- `{m.record.name}` ({_size(m.record.size)}) — {m.reason}")
        L.append("")

    if plan.review or plan.folder_review:
        L.append(f"## Waiting for review ({plan.review_count})")
        L.append("")
        L.append("Run `organize review`, or /review in Telegram, to decide on these.")
        L.append("")
        for df in plan.folder_review:
            line = f"- 📁 `{df.folder.rel}/` ({df.folder.file_count} files) — {df.why}"
            if df.suggestion:
                line += f" → suggested `{df.suggestion}`"
            L.append(line)
        for d in sorted(plan.review, key=lambda x: x.record.name.lower()):
            bits = [f"- `{d.record.name}` — {d.why}"]
            if d.suggestion:
                bits.append(f" → suggested `{d.suggestion}`")
            L.append("".join(bits))
        L.append("")

    if scan.skipped:
        L.append(f"<details><summary>Skipped ({len(scan.skipped)})</summary>")
        L.append("")
        for p, why in sorted(scan.skipped, key=lambda x: x[0].name.lower()):
            L.append(f"- `{p.name}` — {why}")
        L.append("")
        L.append("</details>")
        L.append("")

    if scan.pruned_dirs:
        L.append(f"<details><summary>Directories left intact "
                 f"({len(scan.pruned_dirs)})</summary>")
        L.append("")
        for p, why in scan.pruned_dirs:
            L.append(f"- `{p.name}/` — {why}")
        L.append("")
        L.append("</details>")
        L.append("")

    if attempts:
        used = [a for a in attempts if a.ok]
        failed = [a for a in attempts if not a.ok]
        L.append("<details><summary>Provider attempts</summary>")
        L.append("")
        for a in used:
            L.append(f"- ✅ `{a.provider}/{a.model}` attempt {a.attempt} "
                     f"({a.elapsed:.1f}s)")
        for a in failed:
            L.append(f"- ❌ `{a.provider}/{a.model}` attempt {a.attempt} — "
                     f"{a.error_type}: {a.message[:120]}")
        L.append("")
        L.append("</details>")
        L.append("")

    if applied and applied.errors:
        L.append("## Errors")
        L.append("")
        for path, err in applied.errors:
            L.append(f"- `{Path(path).name}` — {err}")
        L.append("")

    if not dry_run and (plan.auto or plan.duplicates):
        L.append("---")
        L.append("")
        L.append(f"Undo everything from this run: `organize undo {run_id}`")
        L.append("")

    out.write_text("\n".join(L), encoding="utf-8")
    return out
