"""Is this thing actually running?

The failure that motivated this module was silent: the launchd jobs were
deregistered, nothing crashed, nothing logged, and the organizer simply stopped
for two days without anyone noticing. Absence of errors is not evidence of
working, so these checks look for *positive* signs of life.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from . import host, journal, telegram, trash
from .config import Config

# The bot long-polls every ~50s and records each successful round trip.
BOT_SILENT_AFTER_MINUTES = 15

# A run is scheduled daily. Allow a generous margin for a computer that was
# asleep or switched off before calling it a missed run.
STALE_AFTER_HOURS = 36


def job_states():
    """Per-job status from whichever scheduler this OS uses."""
    return host.scheduler().job_states()


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
    return host.sleep_risk()


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
        # Running is not the same as working: a bot that can't reach Telegram
        # is still a live process. Its heartbeat says whether it got through.
        if st.job == "bot" and st.running:
            from .bot import last_heartbeat
            beat = last_heartbeat(cfg)
            if beat is None or time.time() - beat > BOT_SILENT_AFTER_MINUTES * 60:
                ago = ("never" if beat is None
                       else f"{(time.time() - beat) / 60:.0f} min ago")
                lines.append(f"PROBLEM  bot is running but hasn't reached "
                             f"Telegram since {ago} — check the network")
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
        lines.append(f"warn     deleted files recover via {host.RESTORE_HINT}"
                     + (" (no Full Disk Access, so undo can't reach the Trash)"
                        if host.IS_MAC else ""))

    risky, why = sleep_risk()
    lines.append(("warn     " if risky else "ok       ") + why)
    return healthy, lines
