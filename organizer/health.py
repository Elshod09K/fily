"""Is this thing actually running?

The failure that motivated this module was silent: the launchd jobs were
deregistered, nothing crashed, nothing logged, and the organizer simply stopped
for two days without anyone noticing. Absence of errors is not evidence of
working, so these checks look for *positive* signs of life.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import journal, launchd, telegram, trash
from .config import Config

JOBS = launchd.JOBS
AGENTS = launchd.AGENTS

# A run is scheduled daily. Allow a generous margin for a Mac that was asleep
# or switched off before calling it a missed run.
STALE_AFTER_HOURS = 36


@dataclass
class JobState:
    job: str
    label: str
    plist_installed: bool
    registered: bool
    running: bool
    last_exit: int | None
    schedule: str = ""

    @property
    def ok(self) -> bool:
        if not (self.plist_installed and self.registered):
            return False
        if self.job == "bot":
            return self.running
        return self.last_exit in (None, 0)

    @property
    def problem(self) -> str:
        if not self.plist_installed:
            return f"no plist in {AGENTS}"
        if not self.registered:
            return "not registered with launchd — it will never fire"
        if self.job == "bot" and not self.running:
            return "not running, so Telegram will not answer"
        if self.last_exit not in (None, 0):
            return f"last run exited {self.last_exit}"
        return ""


def _launchctl_list() -> dict[str, tuple[int | None, int | None]]:
    """label -> (pid, last exit). pid None means loaded but not running."""
    out: dict[str, tuple[int | None, int | None]] = {}
    try:
        r = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                           text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return out
    for line in r.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, status, label = parts[0], parts[1], parts[2]
        out[label] = (None if pid == "-" else int(pid),
                      None if status == "-" else int(status))
    return out


def _schedule_of(plist: Path) -> str:
    try:
        d = plistlib.loads(plist.read_bytes())
    except (OSError, plistlib.InvalidFileException):
        return ""
    if d.get("KeepAlive"):
        return "always on"
    cal = d.get("StartCalendarInterval")
    if isinstance(cal, dict):
        return f"{cal.get('Hour', 0):02d}:{cal.get('Minute', 0):02d} daily"
    return ""


def job_states() -> list[JobState]:
    listing = _launchctl_list()
    states: list[JobState] = []
    for job in JOBS:
        label = launchd.label(job)
        plist = launchd.plist_path(job)
        pid, exit_code = listing.get(label, (None, None))
        states.append(JobState(
            job=job, label=label,
            plist_installed=plist.exists(),
            registered=label in listing,
            running=pid is not None,
            last_exit=exit_code,
            schedule=_schedule_of(plist) if plist.exists() else "",
        ))
    return states


@dataclass
class RunHealth:
    last_run_id: str | None
    last_ok_at: float | None
    hours_since: float | None
    status: str | None

    @property
    def stale(self) -> bool:
        return self.hours_since is None or self.hours_since > STALE_AFTER_HOURS


def run_health(cfg: Config) -> RunHealth:
    cache = journal.Cache(cfg)
    runs = cache.recent_runs(20)
    cache.close()
    real = [r for r in runs if r["status"] in ("ok", "failed", "timeout")]
    if not real:
        return RunHealth(None, None, None, None)
    latest = real[0]
    ok = next((r for r in real if r["status"] == "ok"), None)
    at = (ok or latest)["started_at"]
    return RunHealth(
        last_run_id=latest["run_id"],
        last_ok_at=at,
        hours_since=(time.time() - at) / 3600.0 if at else None,
        status=latest["status"],
    )


def sleep_risk() -> tuple[bool, str]:
    """Will the nightly job actually fire, or be deferred by sleep?

    launchd never runs a calendar job while the Mac is asleep; it runs it on
    the next wake instead. So the real question is whether this Mac sleeps.
    """
    try:
        r = subprocess.run(["/usr/bin/pmset", "-g", "custom"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return True, "could not read power settings"

    section, sleeps = None, {}
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith("Battery Power"):
            section = "battery"
        elif s.startswith("AC Power"):
            section = "ac"
        elif section and s.startswith("sleep "):
            try:
                sleeps[section] = int(s.split()[1])
            except (IndexError, ValueError):
                pass

    scheduled = _scheduled_wake()
    ac, batt = sleeps.get("ac"), sleeps.get("battery")
    bits = []
    if ac == 0:
        bits.append("on AC it never sleeps, so runs fire on time")
    elif ac:
        bits.append(f"on AC it sleeps after {ac} min")
    if batt == 0:
        bits.append("on battery it never sleeps")
    elif batt:
        bits.append(f"on battery it sleeps after {batt} min")

    if scheduled:
        bits.append(f"a daily wake is scheduled at {scheduled}")
        return False, "; ".join(bits)
    risky = bool(batt) or bool(ac)
    if risky:
        bits.append("with no scheduled wake, a run starting while asleep is "
                    "deferred to the next wake")
    return risky, "; ".join(bits)


def _scheduled_wake() -> str | None:
    try:
        r = subprocess.run(["/usr/bin/pmset", "-g", "sched"],
                           capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    # `pmset -g sched` prints two sections. Only the "Repeating power events"
    # one counts: the "Scheduled power events" list is mostly Apple's own
    # invisible maintenance alarms, which do nothing for us.
    in_repeating = False
    for line in r.stdout.splitlines():
        stripped = line.strip()
        low = stripped.lower()
        if low.startswith("repeating power events"):
            in_repeating = True
            continue
        if low.startswith("scheduled power events"):
            in_repeating = False
            continue
        if in_repeating and stripped and ("wake" in low or "poweron" in low):
            return stripped
    return None


def summary(cfg: Config) -> tuple[bool, list[str]]:
    """(healthy, human readable lines)."""
    lines: list[str] = []
    healthy = True

    for st in job_states():
        if st.job == "bot" and not telegram.configured():
            continue
        mark = "ok" if st.ok else "PROBLEM"
        detail = st.schedule or ""
        if st.job == "bot" and st.running:
            detail = "always on, running"
        lines.append(f"{mark:<8} {st.job:<6} {detail}"
                     + (f" — {st.problem}" if st.problem else ""))
        if not st.ok:
            healthy = False

    from .scanner import root_readable
    for root in cfg.scan_roots:
        ok_root, why = root_readable(root)
        if not ok_root:
            lines.append(f"PROBLEM  cannot read {root}: {why}")
            healthy = False

    rh = run_health(cfg)
    if rh.hours_since is None:
        lines.append("PROBLEM  no run has ever completed")
        healthy = False
    elif rh.stale:
        lines.append(f"PROBLEM  last successful run was "
                     f"{rh.hours_since:.0f}h ago (expected daily)")
        healthy = False
    else:
        lines.append(f"ok       last successful run {rh.hours_since:.0f}h ago")

    paired = telegram.load_chat_id(cfg.state_dir)
    if telegram.configured():
        lines.append(f"ok       telegram paired to chat {paired}" if paired
                     else "PROBLEM  telegram not paired — run `organize bot --pair`")
        if not paired:
            healthy = False
        from .notify import last_delivery
        d = last_delivery(cfg)
        if d:
            ago = (time.time() - d["at"]) / 3600.0
            when = f"{ago:.0f}h ago" if ago >= 1 else f"{ago * 60:.0f}m ago"
            if d["ok"]:
                lines.append(f"ok       last Telegram message delivered {when}")
            else:
                lines.append(f"PROBLEM  last Telegram message FAILED {when}: "
                             f"{d.get('why') or 'unknown'}")
                healthy = False
    else:
        lines.append("—        telegram not configured")

    if trash.trash_readable():
        lines.append("ok       deleted files can be restored by `organize undo`")
    else:
        lines.append("warn     deleted files recover via Finder \u2192 Put Back "
                     "(no Full Disk Access, so undo cannot reach the Trash)")

    risky, why = sleep_risk()
    lines.append(("warn     " if risky else "ok       ") + why)
    return healthy, lines
