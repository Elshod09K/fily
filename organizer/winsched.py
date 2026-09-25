"""Task Scheduler backend: the Windows counterpart of launchd.py.

Three tasks in a "Fily" folder of the Task Scheduler Library, created per-user
(no admin rights needed). Several Task Scheduler defaults are wrong for a
laptop and are overridden explicitly, because each would fail silently:

  * DisallowStartIfOnBatteries / StopIfGoingOnBatteries default to true, so
    on an unplugged laptop the run would simply never happen.
  * StartWhenAvailable defaults to false, so a run missed while the laptop was
    off or asleep would be skipped instead of caught up — the behaviour launchd
    gives the Mac for free.
  * WakeToRun defaults to false.

Tasks run pythonw.exe (no console window) with -X utf8, so non-English file
names never meet a legacy code page, and log to a file, since pythonw has no
stdout at all.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .config import PROJECT_ROOT, Config
from .host import windows as win
from .host.base import JOBS, InstallResult, JobState

# Overridable so CI can register throwaway tasks without touching real ones.
TASK_FOLDER = os.environ.get("FILY_TASK_FOLDER", "Fily")

# Task Scheduler result codes that aren't really an exit status.
_STILL_RUNNING = 0x41301
_NEVER_RAN = 0x41303
_NOT_A_RESULT = {_STILL_RUNNING, _NEVER_RAN}


def label(job: str) -> str:
    return f"\\{TASK_FOLDER}\\{job}"


def pythonw() -> Path:
    """This copy's pythonw.exe — the windowless interpreter."""
    here = Path(sys.executable).with_name("pythonw.exe")
    if here.exists():
        return here
    return PROJECT_ROOT / ".venv" / "Scripts" / "pythonw.exe"


def log_path(job: str, cfg: Config) -> Path:
    return cfg.state_dir / "logs" / f"{job}.log"


def arguments(job: str, cfg: Config) -> str:
    return f'-X utf8 -m organizer.cli --log "{log_path(job, cfg)}" {job}'


def build_xml(job: str, cfg: Config, user: str) -> str:
    """The task definition. `user` is the account's SID (or DOMAIN\\name)."""
    s = cfg.schedule
    start = "2026-01-01"
    u = escape(user)
    if job == "bot":
        # At logon, plus every 5 minutes as a safety net: with IgnoreNew a
        # running bot is left alone, and a bot that died for any reason —
        # including its own deliberate exit when its code changes — is back
        # within minutes even if restart-on-failure gave up.
        triggers = f"""
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{u}</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start}T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>"""
        limit, wake = "PT0S", "false"
        restart = """
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>99</Count>
    </RestartOnFailure>"""
    else:
        h, m = ((s.run_hour, s.run_minute) if job == "run"
                else (s.alert_hour, s.alert_minute))
        triggers = f"""
    <CalendarTrigger>
      <StartBoundary>{start}T{h:02d}:{m:02d}:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>"""
        limit = "PT1H"
        wake = "true" if job == "run" else "false"
        restart = ""

    what = {"run": "organizes your folders", "alert": "morning check-in",
            "bot": "Telegram bot"}[job]
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Fily — {what}. Remove with: organize install --uninstall</Description>
    <URI>{escape(label(job))}</URI>
  </RegistrationInfo>
  <Triggers>{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{u}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>{wake}</WakeToRun>
    <ExecutionTimeLimit>{limit}</ExecutionTimeLimit>
    <Priority>7</Priority>{restart}
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>"{escape(str(pythonw()))}"</Command>
      <Arguments>{escape(arguments(job, cfg))}</Arguments>
      <WorkingDirectory>{escape(str(PROJECT_ROOT))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _user() -> str:
    sid = win.current_user_sid()
    if sid:
        return sid
    dom, name = os.environ.get("USERDOMAIN", ""), os.environ.get("USERNAME", "")
    return f"{dom}\\{name}" if dom else name


def _schtasks(*args: str):
    return win._run(["schtasks", *args], timeout=60)


def _text(r) -> str:
    out = r.stdout or b""
    err = r.stderr or b""
    if isinstance(out, bytes):
        out = out.decode("utf-8", "replace")
    if isinstance(err, bytes):
        err = err.decode("utf-8", "replace")
    return (err or out).strip()


def install(cfg: Config, jobs: tuple[str, ...] = JOBS) -> InstallResult:
    (cfg.state_dir / "logs").mkdir(parents=True, exist_ok=True)
    xml_dir = cfg.state_dir / "tasks"
    xml_dir.mkdir(parents=True, exist_ok=True)
    result = InstallResult()
    user = _user()

    for job in set(JOBS) - set(jobs):
        _schtasks("/Delete", "/TN", label(job), "/F")

    for job in jobs:
        xml_file = xml_dir / f"{job}.xml"
        # schtasks reliably accepts only UTF-16 task XML.
        xml_file.write_text(build_xml(job, cfg, user), encoding="utf-16")
        r = _schtasks("/Create", "/TN", label(job), "/XML", str(xml_file), "/F")
        if r.returncode == 0:
            result.installed.append(job)
        else:
            result.failed.append(f"{job}: {_text(r)[:200]}")

    # launchd starts the bot on load; Task Scheduler would wait for the next
    # logon or the 5-minute trigger, so start it now.
    if "bot" in result.installed:
        restart("bot")
    return result


def uninstall() -> list[str]:
    removed = []
    for job in JOBS:
        r = _schtasks("/Delete", "/TN", label(job), "/F")
        if r.returncode == 0:
            removed.append(label(job))
    # schtasks cannot remove the (now empty) folder; the COM API can.
    win.powershell(
        "$s = New-Object -ComObject Schedule.Service; $s.Connect(); "
        f"try {{ $s.GetFolder('\\').DeleteFolder('{TASK_FOLDER}', 0) }} catch {{}}",
        timeout=30)
    return removed


def restart(job: str) -> bool:
    _schtasks("/End", "/TN", label(job))
    return _schtasks("/Run", "/TN", label(job)).returncode == 0


_QUERY = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($t in Get-ScheduledTask -TaskPath "\$env:FILY_FOLDER\") {
  $i = $t | Get-ScheduledTaskInfo
  $out += [pscustomobject]@{
    name        = $t.TaskName
    state       = $t.State.ToString()
    last_result = [int64]$i.LastTaskResult
    wake        = [bool]$t.Settings.WakeToRun
    battery_ok  = -not [bool]$t.Settings.DisallowStartIfOnBatteries
    catch_up    = [bool]$t.Settings.StartWhenAvailable
    starts      = @($t.Triggers | ForEach-Object { [string]$_.StartBoundary })
  }
}
ConvertTo-Json -InputObject $out -Compress -Depth 4
"""


def query() -> dict[str, dict]:
    """Task name → properties, straight from Task Scheduler. Uses PowerShell
    objects rather than parsing `schtasks /Query`, whose output is localized."""
    r = win.powershell(_QUERY, env={"FILY_FOLDER": TASK_FOLDER}, timeout=60)
    return parse_query(r.stdout)


def parse_query(text: str) -> dict[str, dict]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    if isinstance(data, dict):
        data = [data]
    return {d["name"]: d for d in data if isinstance(d, dict) and d.get("name")}


def to_state(job: str, info: dict | None) -> JobState:
    where = f"Task Scheduler → {TASK_FOLDER}"
    if info is None:
        return JobState(job=job, label=label(job), installed=False,
                        registered=False, running=False, last_exit=None,
                        where=where)
    code = info.get("last_result")
    if isinstance(code, int) and code < 0:
        code &= 0xFFFFFFFF
    last_exit = None if code in _NOT_A_RESULT or code is None else code
    schedule = "always on"
    if job != "bot":
        starts = info.get("starts") or []
        try:
            t = datetime.fromisoformat(str(starts[0])[:19])
            schedule = f"{t:%H:%M} daily"
        except (IndexError, ValueError):
            schedule = "daily"
    return JobState(job=job, label=label(job), installed=True, registered=True,
                    running=info.get("state") == "Running",
                    last_exit=last_exit, schedule=schedule, where=where)


def job_states() -> list[JobState]:
    q = query()
    return [to_state(job, q.get(job)) for job in JOBS]
