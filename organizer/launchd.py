"""Generate, install and remove the launchd jobs.

The plists are built at install time from wherever this copy actually lives,
so the repository contains no machine-specific paths and no secrets. Keys are
read from the project's chmod-600 .env by the app itself, never passed through
launchd, because a LaunchAgent plist is world-readable and gets backed up.
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import PROJECT_ROOT, Config
from .host.base import JOBS, InstallResult, JobState

LABEL_PREFIX = "local.fily"
AGENTS = Path.home() / "Library" / "LaunchAgents"

# Label prefixes used by earlier builds. If the prefix ever changes, add the
# old one here so upgraded machines drop their old jobs instead of running two
# copies of everything.
LEGACY_PREFIXES: tuple[str, ...] = ()


def label(job: str) -> str:
    return f"{LABEL_PREFIX}.{job}"


def plist_path(job: str) -> Path:
    return AGENTS / f"{label(job)}.plist"


def entrypoint() -> Path:
    """The `organize` script inside this copy's virtualenv."""
    candidate = Path(sys.executable).parent / "organize"
    if candidate.exists():
        return candidate
    return PROJECT_ROOT / ".venv" / "bin" / "organize"


def build(job: str, cfg: Config) -> dict:
    logs = cfg.state_dir / "logs"
    d: dict = {
        "Label": label(job),
        "ProgramArguments": [str(entrypoint()), job],
        "EnvironmentVariables": {
            "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONUNBUFFERED": "1",
        },
        "WorkingDirectory": str(PROJECT_ROOT),
        "StandardOutPath": str(logs / f"{job}.stdout.log"),
        "StandardErrorPath": str(logs / f"{job}.stderr.log"),
        "ProcessType": "Background",
        "LowPriorityIO": True,
    }
    if job == "bot":
        # Always on; launchd restarts it after a crash, a network drop, a
        # reboot, or its own deliberate exit when its source changes.
        d.update({"RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 20})
    else:
        s = cfg.schedule
        hour, minute = ((s.run_hour, s.run_minute) if job == "run"
                        else (s.alert_hour, s.alert_minute))
        d.update({"RunAtLoad": False, "ThrottleInterval": 300,
                  "StartCalendarInterval": {"Hour": hour, "Minute": minute}})
    return d


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["/bin/launchctl", *args], capture_output=True, text=True, encoding="utf-8")


def _domain() -> str:
    return f"gui/{os.getuid()}"


def remove_legacy() -> list[str]:
    removed = []
    for prefix in LEGACY_PREFIXES:
        for job in JOBS:
            old = f"{prefix}.{job}"
            _launchctl("bootout", f"{_domain()}/{old}")
            f = AGENTS / f"{old}.plist"
            if f.exists():
                f.unlink()
                removed.append(old)
    return removed


def install(cfg: Config, jobs: tuple[str, ...] = JOBS) -> InstallResult:
    """Write and register the jobs.

    Must be run from a real login session (Terminal). launchctl registers into
    the caller's domain, so bootstrapping from a sandboxed or short-lived
    process yields jobs that silently vanish when it exits.
    """
    (cfg.state_dir / "logs").mkdir(parents=True, exist_ok=True)
    AGENTS.mkdir(parents=True, exist_ok=True)
    result = InstallResult(removed_legacy=remove_legacy())

    # A job not being installed this time (the bot, when Telegram was
    # skipped) must not linger from an earlier install.
    for job in set(JOBS) - set(jobs):
        _launchctl("bootout", f"{_domain()}/{label(job)}")
        plist_path(job).unlink(missing_ok=True)

    for job in jobs:
        dst = plist_path(job)
        _launchctl("bootout", f"{_domain()}/{label(job)}")
        dst.write_bytes(plistlib.dumps(build(job, cfg)))
        r = _launchctl("bootstrap", _domain(), str(dst))
        if r.returncode == 0:
            result.installed.append(job)
        else:
            result.failed.append(f"{job}: {(r.stderr or r.stdout).strip()[:160]}")
    return result


def uninstall() -> list[str]:
    removed = remove_legacy()
    for job in JOBS:
        _launchctl("bootout", f"{_domain()}/{label(job)}")
        f = plist_path(job)
        if f.exists():
            f.unlink()
            removed.append(label(job))
    return removed


def restart(job: str) -> bool:
    r = _launchctl("kickstart", "-k", f"{_domain()}/{label(job)}")
    return r.returncode == 0


# ------------------------------------------------------------------ status

def _launchctl_list() -> dict[str, tuple[int | None, int | None]]:
    """label -> (pid, last exit). pid None means loaded but not running."""
    out: dict[str, tuple[int | None, int | None]] = {}
    try:
        r = subprocess.run(["/bin/launchctl", "list"], capture_output=True,
                           text=True, timeout=15, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired):
        return out
    for line in r.stdout.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        pid, status, lbl = parts[0], parts[1], parts[2]
        try:
            out[lbl] = (None if pid == "-" else int(pid),
                        None if status == "-" else int(status))
        except ValueError:
            continue
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
        lbl, plist = label(job), plist_path(job)
        pid, exit_code = listing.get(lbl, (None, None))
        states.append(JobState(
            job=job, label=lbl, installed=plist.exists(),
            registered=lbl in listing, running=pid is not None,
            last_exit=exit_code,
            schedule=_schedule_of(plist) if plist.exists() else "",
            where=str(AGENTS)))
    return states
