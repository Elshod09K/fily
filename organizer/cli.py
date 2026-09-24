"""Command line entry point.

    organize doctor          check keys, providers, permissions, scope
    organize run             the nightly job (scan, classify, apply, report)
    organize review          decide on the queued ambiguous files
    organize undo            reverse a run
    organize status          recent runs and pending alerts
    organize alert           deliver any queued failure notification
    organize prune           send reviewed duplicates to the Trash
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from rich.console import Console
from rich.table import Table

from . import config as cfgmod
from . import lock as runlock
from . import applier, classify, dedupe, extract, health, journal, notify, planner, report, safety, scanner
from .providers.base import AllProvidersFailed

console = Console()


def _log(msg: str) -> None:
    # Flush every line: under launchd (and any redirect) stdout is block
    # buffered, which would hide retry progress until the run ended.
    console.print(msg, highlight=False)
    console.file.flush()


def _load(args) -> cfgmod.Config:
    path = Path(args.config).expanduser() if getattr(args, "config", None) else None
    cfg = cfgmod.load(path)
    if getattr(args, "root", None):
        roots = tuple(cfgmod.expand(r) for r in args.root)
        for r in roots:
            why = safety.root_rejection_reason(r, cfg)
            if why:
                console.print(f"[red]refusing --root {r}: {why}[/red]")
                sys.exit(2)
        object.__setattr__(cfg, "scan_roots", roots)
    return cfg


# --------------------------------------------------------------------------- doctor

def cmd_doctor(args) -> int:
    cfg = _load(args)
    ok = True

    console.print("[bold]Configuration[/bold]")
    console.print(f"  config     {cfg.path}")
    console.print(f"  state      {cfg.state_dir}")
    console.print(f"  scan depth {cfg.behaviour.scan_depth} "
                  "(loose files only; existing subfolders left intact)")
    console.print("  roots:")
    for r in cfg.scan_roots:
        console.print(f"    {r}")
    console.print("  media destinations:")
    for k, v in cfg.media_destinations.items():
        console.print(f"    {k:<6} -> {v}")
    console.print("  never touched:")
    for p in (*cfg.deny_paths, *safety.LIBRARY_ROOTS, Path.home() / "Library"):
        console.print(f"    {p}")

    console.print("\n[bold]Permissions[/bold]")
    for r in cfg.scan_roots:
        try:
            next(iter(os.scandir(r)), None)
            console.print(f"  [green]ok[/green]      readable  {r}")
        except PermissionError:
            ok = False
            console.print(f"  [red]DENIED[/red]  {r}")
            console.print("          Grant Full Disk Access to:")
            console.print(f"          [bold]{Path(sys.executable).resolve()}[/bold]")
            console.print("          System Settings > Privacy & Security > "
                          "Full Disk Access")
        except OSError as e:
            console.print(f"  [yellow]warn[/yellow]    {r}: {e}")

    console.print("\n[bold]Providers[/bold]")
    from .providers.gemini import GeminiProvider
    from .providers.nvidia import NvidiaProvider
    impls = {"gemini": GeminiProvider(), "nvidia": NvidiaProvider()}
    reachable: dict[str, list[str]] = {}
    for name, p in impls.items():
        if not p.available():
            ok = False
            env = "GEMINI_API_KEY" if name == "gemini" else "NVIDIA_API_KEY"
            console.print(f"  [red]{name}[/red]: {env} not set in this environment")
            continue
        try:
            models = p.list_models()
            reachable[name] = models
            console.print(f"  [green]{name}[/green]: reachable, "
                          f"{len(models)} models available")
        except Exception as e:
            ok = False
            console.print(f"  [red]{name}[/red]: {type(e).__name__}: {str(e)[:150]}")

    console.print("\n[bold]Failover chain[/bold]")
    console.print("[dim]  probing each link with a real request — a catalogue "
                  "listing is not proof a model answers[/dim]")
    probe_sys = ('Reply with a JSON array and nothing else: '
                 '[{"file_id":0,"category":"test"}]')
    probe_usr = 'Classify: <file id="0">name: example.pdf</file>'
    live = 0
    for i, step in enumerate(cfg.chain, 1):
        impl = impls.get(step.provider)
        if impl is None or not impl.available():
            console.print(f"  {i}. [yellow]?[/yellow] {step.provider}/{step.model} "
                          f"x{step.attempts}  — provider unavailable, not probed")
            continue
        t0 = time.monotonic()
        try:
            impl.complete_json(probe_sys, probe_usr, step.model, 45)
            dt = time.monotonic() - t0
            live += 1
            slow = "  [yellow](slow)[/yellow]" if dt > 15 else ""
            console.print(f"  {i}. [green]ok[/green] {step.provider}/{step.model} "
                          f"x{step.attempts}  — answered in {dt:.1f}s{slow}")
        except Exception as e:
            console.print(f"  {i}. [red]x[/red] {step.provider}/{step.model} "
                          f"x{step.attempts}  — {type(e).__name__}: {str(e)[:110]}")
    if live == 0:
        ok = False
        console.print("  [red]no working link in the chain; a run would move "
                      "nothing and queue an alert[/red]")
    elif live == 1:
        console.print("  [yellow]only one working link — no real fallback[/yellow]")

    console.print("\n[bold]Telegram[/bold]")
    from . import telegram as tg
    if not tg.configured():
        console.print("  [yellow]TELEGRAM_BOT_TOKEN not set — bot disabled[/yellow]")
    else:
        try:
            me = tg.call("getMe", timeout=20, raise_on_error=True)
            console.print(f"  [green]ok[/green]      reachable as "
                          f"@{(me or {}).get('username','?')}")
            hook = tg.call("getWebhookInfo", timeout=20) or {}
            if hook.get("url"):
                ok = False
                console.print(f"  [red]webhook set to {hook['url']}[/red] — "
                              "this blocks polling; clear it with deleteWebhook")
            pending = hook.get("pending_update_count", 0)
            if pending:
                console.print(f"  [yellow]{pending} update(s) queued[/yellow] — "
                              "the bot is not consuming them")
            paired = tg.load_chat_id(cfg.state_dir)
            console.print(f"  [green]ok[/green]      paired to chat {paired}"
                          if paired else
                          "  [yellow]not paired[/yellow] — send /start to the bot")
        except Exception as e:
            ok = False
            console.print(f"  [red]unreachable[/red]: {type(e).__name__}: "
                          f"{str(e)[:160]}")
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                console.print("  [dim]Python cannot verify TLS. This venv should "
                              "use certifi; check it is installed.[/dim]")

    console.print("\n[bold]Scope[/bold]")
    res = scanner.scan(cfg)
    console.print(f"  {len(res.files)} loose files eligible")
    console.print(f"  {len(res.skipped)} skipped (quarantine, hidden, transient)")
    console.print(f"  {len(res.pruned_dirs)} directories left intact")

    pending = notify.alert_path(cfg)
    if pending.exists():
        console.print(f"\n[yellow]There is an undelivered failure alert:[/yellow] "
                      f"{pending}")

    console.print(f"\n{'[green]ready[/green]' if ok else '[red]not ready[/red]'}")
    return 0 if ok else 1


# ------------------------------------------------------------------------------ run

def cmd_run(args) -> int:
    cfg = _load(args)
    try:
        with runlock.run_lock(cfg.state_dir, owner=getattr(args, "owner", "cli")):
            return _run_locked(cfg, args)
    except runlock.Busy as e:
        console.print(f"[yellow]another run is in progress: {e}[/yellow]")
        return 5


def _run_locked(cfg, args) -> int:
    started = time.monotonic()
    run_id = journal.new_run_id()
    dry = bool(args.dry_run)

    console.rule(f"run {run_id}{' (dry run)' if dry else ''}")

    res = scanner.scan(cfg)
    if res.unreadable_roots:
        _report_unreadable(cfg, run_id, res.unreadable_roots,
                           all_blocked=len(res.unreadable_roots) == len(cfg.scan_roots))
        if len(res.unreadable_roots) == len(cfg.scan_roots):
            cache = journal.Cache(cfg)
            cache.start_run(run_id, 0)
            cache.finish_run(run_id, "failed", 0, 0, "no scan root readable")
            cache.close()
            return 6
    _log(f"scanned {len(res.files)} loose files "
         f"({len(res.skipped)} skipped, {len(res.pruned_dirs)} dirs left intact)")
    if not res.files:
        _log("nothing to organise")
        return 0

    cache = journal.Cache(cfg)
    cache.start_run(run_id, len(res.files))

    _log("hashing and looking for exact duplicates...")
    groups, _ = dedupe.find_duplicates(res.files)
    dedupe.ensure_hashes(res.files)
    if groups:
        wasted = sum(g.wasted_bytes for g in groups)
        _log(f"  {len(groups)} duplicate group(s), "
             f"{sum(len(g.duplicates) for g in groups)} redundant copies "
             f"({wasted/1048576:.0f} MB)")

    dup_ids = {d.file_id for g in groups for d in g.duplicates}
    to_classify = [r for r in res.files if r.file_id not in dup_ids]

    _log("extracting text...")
    for r in to_classify:
        extract.enrich(r, cfg.behaviour.snippet_chars)

    _log(f"classifying {len(to_classify)} files...")
    try:
        deadline = started + cfg.behaviour.run_budget_seconds
        decisions, attempts = classify.classify(
            cfg, to_classify, res.existing_folders, cache=cache, log=_log,
            deadline=deadline)
    except AllProvidersFailed as e:
        summary = "classification failed on every provider; no files were moved"
        detail = {
            "error": str(e),
            "attempts": [
                {"provider": a.provider, "model": a.model, "attempt": a.attempt,
                 "error": a.error_type, "message": a.message} for a in e.attempts
            ],
            "files_untouched": len(res.files),
        }
        notify.queue_alert(cfg, run_id, summary, detail)
        cache.finish_run(run_id, "failed", 0, 0, str(e)[:500])
        cache.close()
        console.print(f"[red]{summary}[/red]")
        console.print(f"[dim]{e}[/dim]")
        console.print("An alert is queued and will be delivered by the morning job.")
        return 3

    unclassified = [r for r in to_classify if r.file_id not in decisions]
    if unclassified:
        _log(f"  {len(unclassified)} file(s) left unclassified -> review")

    plan = planner.build_plan(cfg, res.files, decisions, groups)
    _log(f"plan: {len(plan.auto)} auto, {len(plan.duplicates)} duplicates, "
         f"{len(plan.review)} for review")
    for n in plan.notes:
        console.print(f"[yellow]{n}[/yellow]")

    jr = journal.Journal(cfg, run_id)
    applied = applier.apply_moves(cfg, plan.auto + plan.duplicates, jr, dry_run=dry)

    queue = cfg.state_dir / "review_queue.json"
    queue.write_text(json.dumps({
        "run_id": run_id,
        "items": [{
            "path": str(d.record.path), "root": str(d.record.root),
            "why": d.why, "suggestion": d.suggestion,
            "category": d.category, "confidence": d.confidence,
            # Kept so a review card can show what the file actually contains.
            # Already extracted during classification, so this costs nothing.
            "size": d.record.size,
            "kind": d.record.extra.get("kind", ""),
            "snippet": (d.record.snippet or "")[:400],
            "snippet_note": d.record.snippet_note or "",
        } for d in plan.review],
    }, indent=2, ensure_ascii=False), encoding="utf-8")

    rpt = report.write_report(cfg, run_id, res, plan, applied, dry, attempts, groups)
    cache.finish_run(run_id, "dry-run" if dry else "ok",
                     applied.moved, len(plan.review))
    journal.Journal.prune(cfg)
    cache.close()

    console.print()
    if dry:
        console.print(f"[bold]dry run:[/bold] {applied.moved} file(s) would move, "
                      f"{len(plan.review)} would wait for review")
    else:
        bits = [f"[bold green]moved {applied.moved}[/bold green]"]
        if applied.trashed:
            bits.append(f"[yellow]{applied.trashed} duplicate(s) to Trash "
                        f"({applied.trashed_bytes/1048576:.0f} MB)[/yellow]")
        bits.append(f"{len(plan.review)} waiting for review")
        if applied.failed:
            bits.append(f"[red]{applied.failed} failed[/red]")
        console.print(", ".join(bits))
        if applied.moved or applied.trashed:
            console.print(f"undo with: [bold]organize undo {run_id}[/bold]")
        from . import telegram as tg
        lines = [f"🗂 <b>Organized {applied.moved} file(s)</b>"]
        by_folder: dict[str, int] = {}
        for m in plan.auto:
            by_folder[m.folder] = by_folder.get(m.folder, 0) + 1
        for folder, n in sorted(by_folder.items(), key=lambda x: -x[1])[:8]:
            lines.append(f"• {tg.escape(folder)}/ — {n}")
        if applied.trashed:
            lines.append(f"🗑 {applied.trashed} duplicate(s) to Trash — "
                         f"{applied.trashed_bytes/1048576:.0f} MB "
                         "<i>(Finder → Put Back to undo)</i>")
        elif plan.duplicates:
            lines.append(f"• _Duplicates/ — {len(plan.duplicates)} "
                         "(nothing deleted)")
        if applied.failed:
            lines.append(f"\n⚠️ {applied.failed} could not be moved")
        buttons = [[{"text": "↩️ Undo this run", "callback_data": "undo:yes"}]]
        if plan.review:
            lines.append(f"\n📋 <b>{len(plan.review)}</b> need your call")
            buttons.insert(0, [{"text": f"📋 Review {len(plan.review)}",
                                "callback_data": "review"}])
        notify.notify(
            cfg, f"Moved {applied.moved}, {len(plan.review)} need review",
            subtitle=f"run {run_id}",
            telegram_text="\n".join(lines), buttons=buttons)
    console.print(f"report: {rpt}")
    return 0


# --------------------------------------------------------------------------- review

def _report_unreadable(cfg, run_id: str, blocked, all_blocked: bool) -> None:
    """Say loudly that macOS is hiding folders from us, and how to fix it."""
    from . import telegram as tg
    interp = Path(sys.executable).resolve()
    for root, why in blocked:
        console.print(f"[red]cannot read {root}: {why}[/red]")
    console.print(f"Grant Full Disk Access to: [bold]{interp}[/bold]")

    names = ", ".join(f"<code>{tg.escape(str(r).replace(str(Path.home()), '~'))}</code>"
                      for r, _ in blocked)
    body = ["🔒 <b>macOS is blocking access to your folders</b>", "",
            f"I could not read: {names}",
            "" if all_blocked else "The other folders were organized normally.",
            "<b>Fix, once:</b> System Settings → Privacy &amp; Security → "
            "<b>Full Disk Access</b> → <b>+</b> → press ⌘⇧G and paste:",
            f"<code>{tg.escape(str(interp))}</code>",
            "", "Then tap /run to try again."]
    summary = ("macOS is blocking access to "
               + ("every folder" if all_blocked else f"{len(blocked)} folder(s)"))
    # Deliver now, not at the morning check: nothing else will fix itself.
    notify.notify(cfg, summary, subtitle="Full Disk Access needed", sound=True,
                  telegram_text="\n".join(l for l in body if l is not None))


def cmd_review(args) -> int:
    cfg = _load(args)
    queue = cfg.state_dir / "review_queue.json"
    if not queue.exists():
        console.print("nothing queued")
        return 0
    data = json.loads(queue.read_text())
    items = [i for i in data.get("items", []) if Path(i["path"]).exists()]
    if not items:
        console.print("nothing queued")
        return 0

    if args.list:
        t = Table(show_header=True, header_style="bold")
        t.add_column("file", overflow="fold")
        t.add_column("why", overflow="fold")
        t.add_column("suggested", overflow="fold")
        for i in items:
            t.add_row(Path(i["path"]).name, i["why"],
                      Path(i["suggestion"]).parent.name if i["suggestion"] else "-")
        console.print(t)
        return 0

    run_id = journal.new_run_id()
    jr = journal.Journal(cfg, run_id)
    allowed = tuple(cfg.scan_roots) + tuple(cfg.media_destinations.values())
    moved = kept = trashed = 0

    for i in items:
        src = Path(i["path"])
        console.print(f"\n[bold]{src.name}[/bold]")
        console.print(f"  in {src.parent}")
        console.print(f"  {i['why']}")
        default = ""
        if i["suggestion"]:
            sug = Path(i["suggestion"])
            default = sug.parent.name if sug.is_absolute() else i["suggestion"]
            console.print(f"  suggested folder: [cyan]{default}[/cyan]")
        try:
            ans = input("  folder (enter=accept, s=skip, d=delete, q=quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\nstopped")
            break
        if ans.lower() == "q":
            break
        if ans.lower() == "d":
            from . import trash as trashmod
            try:
                done = trashmod.send_to_trash(src)
            except trashmod.TrashError as e:
                console.print(f"  [red]{e}[/red]")
                kept += 1
                continue
            jr.record(journal.MoveEntry(
                src=str(done.original), dst=str(done.trashed_to or ""),
                sha256=done.sha, size=done.size, ts=time.time(),
                created_dirs=[], hash_method=done.method, action="trash",
                category=i.get("category", "manual"), confidence=1.0,
                provider="manual"))
            trashed += 1
            console.print("  [yellow]-> Trash[/yellow] (recoverable in Finder)")
            continue
        if ans.lower() == "s" or (not ans and not default):
            kept += 1
            continue
        folder = ans or default
        root = Path(i["root"])
        dest, why = safety.resolve_destination(root, folder, src.name, allowed)
        if dest is None:
            console.print(f"  [red]rejected: {why}[/red]")
            kept += 1
            continue
        move = planner.PlannedMove(
            record=scanner.FileRecord(
                path=src, root=root, size=src.stat().st_size,
                mtime=src.stat().st_mtime, ctime=src.stat().st_mtime,
                ext=src.suffix.lower().lstrip(".")),
            dest=dest, category=i.get("category", "manual"), folder=folder,
            confidence=1.0, reason="chosen during review", provider="manual")
        r = applier.apply_moves(cfg, [move], jr)
        moved += r.moved
        if r.errors:
            console.print(f"  [red]{r.errors[0][1]}[/red]")
        else:
            console.print(f"  [green]-> {folder}/[/green]")

    remaining = [i for i in items if Path(i["path"]).exists()]
    queue.write_text(json.dumps({"run_id": data.get("run_id"), "items": remaining},
                                indent=2, ensure_ascii=False), encoding="utf-8")
    console.print(f"\nmoved {moved}, deleted {trashed}, left {kept}")
    if moved or trashed:
        console.print(f"undo with: [bold]organize undo {run_id}[/bold]")
    return 0


# ----------------------------------------------------------------------------- undo

def cmd_undo(args) -> int:
    cfg = _load(args)
    runs = journal.Journal.list_runs(cfg)
    if not runs:
        console.print("no runs to undo")
        return 1
    if args.run_id and args.last:
        console.print("[red]give a run id or --last, not both[/red]")
        return 2
    if args.run_id:
        target = cfg.state_dir / "journal" / f"{args.run_id}.jsonl"
        if not target.exists():
            console.print(f"[red]no journal for run {args.run_id}[/red]")
            console.print("available: " + ", ".join(p.stem for p in runs[-10:]))
            return 1
    else:
        target = runs[-1]

    entries = journal.read_journal(target)
    if not entries:
        console.print(f"run {target.stem} moved nothing")
        return 0
    console.print(f"reversing {len(entries)} move(s) from run {target.stem}")
    res = applier.undo_run(cfg, target, dry_run=args.dry_run)
    verb = "would restore" if args.dry_run else "restored"
    extra = f" ({res.untrashed} from the Trash)" if res.untrashed else ""
    console.print(f"[green]{verb} {res.restored}{extra}[/green], "
                  f"skipped {res.skipped}"
                  + (f", removed {res.dirs_removed} empty folder(s)"
                     if res.dirs_removed else ""))
    for p, why in res.problems:
        console.print(f"  [yellow]{Path(p).name}[/yellow] — {why}")
    if not args.dry_run and res.restored and not res.problems:
        target.rename(target.with_suffix(".jsonl.undone"))
    return 0


# --------------------------------------------------------------------------- status

def cmd_setup(args) -> int:
    from . import setup
    return setup.run(args)


def cmd_probe_access(args) -> int:
    from . import setup
    return setup.probe_access(Path(args.out))


def cmd_install(args) -> int:
    """Install and arm the scheduled jobs.

    Run from a normal Terminal. launchctl registers into the domain of
    whoever calls it, so bootstrapping from a sandboxed or short-lived process
    produces jobs that vanish when that process does.
    """
    from . import launchd

    if args.uninstall:
        removed = launchd.uninstall()
        for r in removed:
            console.print(f"  [yellow]removed {r}[/yellow]")
        console.print("\nScheduled jobs removed. Nothing will run automatically.")
        return 0

    cfg = _load(args)
    from . import telegram as tg
    jobs = launchd.JOBS if tg.configured() else tuple(
        j for j in launchd.JOBS if j != "bot")
    res = launchd.install(cfg, jobs)
    for old in res.removed_legacy:
        console.print(f"  [dim]removed old job {old}[/dim]")
    for job in res.installed:
        console.print(f"  [green]armed {launchd.label(job)}[/green]")
    for line in res.failed:
        console.print(f"  [red]{line}[/red]")
    console.print()
    if res.ok:
        s = cfg.schedule
        console.print(f"[green]armed[/green] — organizes daily at {s.run_at}, "
                      f"morning check at {s.alert_at}, Telegram bot always on")
    else:
        console.print("[red]some jobs did not register[/red]; run "
                      "[bold]organize health[/bold] for detail")
    return 0 if res.ok else 1


def cmd_health(args) -> int:
    cfg = _load(args)
    ok, lines = health.summary(cfg)
    console.print("[bold]Scheduled jobs[/bold]")
    for line in lines:
        mark = line.split()[0]
        colour = {"ok": "green", "PROBLEM": "red", "warn": "yellow"}.get(mark, "")
        console.print(f"  [{colour}]{line}[/{colour}]" if colour else f"  {line}")
    console.print()
    if ok:
        console.print(f"[green]healthy[/green] — next run at {cfg.schedule.run_at}")
    else:
        console.print("[red]not healthy[/red]")
        console.print("Re-arm the schedule with:")
        console.print("  [bold]organize install[/bold]")
    return 0 if ok else 1


def cmd_status(args) -> int:
    cfg = _load(args)
    cache = journal.Cache(cfg)
    runs = cache.recent_runs(10)
    if runs:
        t = Table(show_header=True, header_style="bold", title="recent runs")
        for c in ("run", "when", "status", "scanned", "moved", "review"):
            t.add_column(c)
        for r in runs:
            when = datetime.fromtimestamp(r["started_at"]).strftime("%m-%d %H:%M") \
                if r["started_at"] else "-"
            style = {"failed": "red", "timeout": "red", "ok": "green"}.get(
                r["status"], "")
            t.add_row(r["run_id"], when, f"[{style}]{r['status']}[/{style}]"
                      if style else r["status"],
                      str(r["scanned"] or 0), str(r["moved"] or 0),
                      str(r["queued"] or 0))
        console.print(t)
    else:
        console.print("no runs yet")
    cache.close()

    queue = cfg.state_dir / "review_queue.json"
    if queue.exists():
        n = len(json.loads(queue.read_text()).get("items", []))
        if n:
            console.print(f"\n{n} file(s) waiting: [bold]organize review[/bold]")

    pending = notify.alert_path(cfg)
    if pending.exists():
        alerts = json.loads(pending.read_text()).get("alerts", [])
        console.print(f"\n[red]{len(alerts)} undelivered failure alert(s)[/red]")
        for a in alerts[-3:]:
            console.print(f"  {a['when']} — {a['summary']}")
    return 0


def cmd_alert(args) -> int:
    cfg = _load(args)
    n, msg = notify.deliver_pending(cfg)
    console.print(f"delivered {n} alert(s): {msg}")

    # Watchdog. The failure this catches is the silent one: jobs quietly
    # deregistered, nothing crashed, nothing logged, and the organizer simply
    # stopped for days. Checked here because this job runs every morning.
    rh = health.run_health(cfg)
    if rh.stale and n == 0:
        hours = f"{rh.hours_since:.0f}h" if rh.hours_since else "ever"
        detail = "has not run since" if rh.hours_since else "has never run"
        console.print(f"[yellow]watchdog: last successful run {hours} ago[/yellow]")
        healthy, lines = health.summary(cfg)
        problems = [l for l in lines if l.startswith("PROBLEM")]
        from . import telegram as tg
        body = ["🔕 <b>The organizer has gone quiet</b>", "",
                f"It {detail} {hours} ago, but should run every day at "
                f"{cfg.schedule.run_at}.",
                ""]
        if problems:
            body.append("<b>What looks wrong:</b>")
            body += [f"• {tg.escape(l.split(None, 1)[1])}" for l in problems]
            body.append("")
        body.append("<i>No files were touched.</i>")
        notify.notify(cfg, f"Organizer has not run in {hours}",
                      subtitle="tap for details", sound=True,
                      telegram_text="\n".join(body),
                      buttons=[[{"text": "▶️ Run now", "callback_data": "run"}]])
    return 0


def cmd_bot(args) -> int:
    cfg = _load(args)
    from . import bot, telegram

    if args.unpair or args.pair:
        if args.unpair:
            telegram.clear_chat_id(cfg.state_dir)
            console.print("unpaired")
        elif telegram.load_chat_id(cfg.state_dir):
            console.print("already paired; use --unpair to move it to another "
                          "account")
            return 0
        code = telegram.new_pairing_code(cfg.state_dir)
        link = telegram.pairing_link(code)
        console.print(f"Open this on your phone to pair:\n  [bold]{link}[/bold]"
                      if link else f"Send this to your bot:  /start {code}")
        return 0
    if not telegram.configured():
        console.print("[red]TELEGRAM_BOT_TOKEN is not set in .env[/red]")
        return 2
    return bot.run_bot(cfg, once=args.once)


def cmd_prune(args) -> int:
    """Send reviewed duplicates to the Trash. Manual, opt-in, recoverable."""
    cfg = _load(args)
    from send2trash import send2trash

    targets: list[Path] = []
    for root in cfg.scan_roots:
        d = root / planner.DUPLICATES_FOLDER
        if d.is_dir():
            targets.extend(p for p in d.iterdir()
                           if p.is_file() and not safety.is_ignored_name(p.name))
    if not targets:
        console.print("no duplicates staged")
        return 0

    total = sum(p.stat().st_size for p in targets)
    console.print(f"{len(targets)} file(s), {total/1048576:.0f} MB in "
                  f"_Duplicates/ folders")
    for p in targets[:20]:
        console.print(f"  {p.name}")
    if len(targets) > 20:
        console.print(f"  ... and {len(targets)-20} more")
    if not args.yes:
        try:
            if input("\nmove these to the Trash? [y/N] ").strip().lower() != "y":
                console.print("cancelled")
                return 0
        except (EOFError, KeyboardInterrupt):
            return 0
    for p in targets:
        send2trash(str(p))
    console.print(f"[green]moved {len(targets)} file(s) to the Trash[/green] "
                  "(recoverable from Finder)")
    return 0


# ------------------------------------------------------------------------------ cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="organize", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", help="path to config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check keys, providers, permissions, scope")
    d.add_argument("--root", action="append", help="override scan roots")
    d.set_defaults(func=cmd_doctor)

    r = sub.add_parser("run", help="scan, classify, apply, report")
    r.add_argument("--dry-run", action="store_true", help="plan without moving")
    r.add_argument("--root", action="append", help="limit to these roots")
    r.set_defaults(func=cmd_run)

    v = sub.add_parser("review", help="decide on queued files")
    v.add_argument("--list", action="store_true", help="show without prompting")
    v.add_argument("--root", action="append")
    v.set_defaults(func=cmd_review)

    u = sub.add_parser("undo", help="reverse a run")
    u.add_argument("run_id", nargs="?", help="run id (default: the most recent)")
    u.add_argument("--last", action="store_true",
                   help="reverse the most recent run (the default; accepted "
                        "because it reads better and is what the docs say)")
    u.add_argument("--dry-run", action="store_true",
                   help="show what would be restored, without touching anything")
    u.set_defaults(func=cmd_undo)

    su = sub.add_parser("setup", help="one-time setup: keys, folders, schedule")
    su.add_argument("--gemini-key")
    su.add_argument("--nvidia-key")
    su.add_argument("--telegram-token")
    su.add_argument("--folders", help="comma-separated, e.g. ~/Downloads,~/Desktop")
    su.add_argument("--time", help="daily run time, e.g. 22:00")
    su.add_argument("--yes", action="store_true",
                    help="non-interactive: take defaults and flags only")
    su.add_argument("--skip-install", action="store_true",
                    help="save settings but schedule nothing")
    su.add_argument("--no-wait", action="store_true",
                    help="print the pairing link but don't wait for it")
    su.set_defaults(func=cmd_setup)

    pa = sub.add_parser("probe-access", help=argparse.SUPPRESS)
    pa.add_argument("--out", required=True)
    pa.set_defaults(func=cmd_probe_access)

    i = sub.add_parser("install", help="install and arm the scheduled jobs")
    i.add_argument("--uninstall", action="store_true",
                   help="stop and remove the scheduled jobs")
    i.set_defaults(func=cmd_install)

    h = sub.add_parser("health", help="is the whole thing actually running?")
    h.set_defaults(func=cmd_health)

    s = sub.add_parser("status", help="recent runs and pending alerts")
    s.set_defaults(func=cmd_status)

    a = sub.add_parser("alert", help="deliver queued failure notifications")
    a.set_defaults(func=cmd_alert)

    b = sub.add_parser("bot", help="run the Telegram bot (long-polling)")
    b.add_argument("--once", action="store_true",
                   help="handle one batch of updates and exit (for testing)")
    b.add_argument("--unpair", action="store_true",
                   help="forget the paired chat and print a fresh pairing link")
    b.add_argument("--pair", action="store_true",
                   help="print a pairing link (for a new phone)")
    b.set_defaults(func=cmd_bot)

    pr = sub.add_parser("prune", help="send staged duplicates to the Trash")
    pr.add_argument("--yes", action="store_true")
    pr.set_defaults(func=cmd_prune)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except cfgmod.ConfigError as e:
        console.print(f"[red]config error:[/red] {e}")
        return 2
    except KeyboardInterrupt:
        console.print("\ninterrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
