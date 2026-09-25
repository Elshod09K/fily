"""Windows support.

Most of these run everywhere: the Task Scheduler XML, result-code handling and
the parsers for localized command output are plain Python. The tests marked
`windows_only` exercise real Win32 behaviour and run on the Windows CI job;
`test_scheduler_end_to_end` also registers and runs real scheduled tasks, so
it only runs where CI opts in with FILY_CI_SCHEDULER=1.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from organizer import config, health, lock
from organizer.host import macos, windows

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="needs Windows")
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "scan_roots": [str(root)], "media_destinations": {}, "deny_paths": [],
        "schedule": {"run": "21:45", "alert": "07:30"},
        "providers": {"chain": [{"provider": "gemini", "model": "x"}]},
    }), encoding="utf-8")
    return config.load(p)


# ------------------------------------------------------------- task XML

def _task(job, cfg, monkeypatch, root="C:/Users/Tom & Jerry/fily"):
    from organizer import winsched
    monkeypatch.setattr(winsched, "PROJECT_ROOT", Path(root))
    monkeypatch.setattr(winsched, "pythonw",
                        lambda: Path(root) / ".venv/Scripts/pythonw.exe")
    xml = winsched.build_xml(job, cfg, "S-1-5-21-1-2-3-1001")
    # schtasks reads UTF-16; parse exactly what it would get.
    return ET.fromstring(xml.encode("utf-16"))


def _setting(tree, name):
    return tree.find(f"t:Settings/t:{name}", NS).text


def test_run_task_survives_a_laptop(cfg, monkeypatch):
    """Task Scheduler defaults would silently skip runs on battery and never
    catch up a run missed while asleep or switched off."""
    t = _task("run", cfg, monkeypatch)
    assert _setting(t, "DisallowStartIfOnBatteries") == "false"
    assert _setting(t, "StopIfGoingOnBatteries") == "false"
    assert _setting(t, "StartWhenAvailable") == "true"
    assert _setting(t, "WakeToRun") == "true"
    assert _setting(t, "MultipleInstancesPolicy") == "IgnoreNew"


def test_run_task_fires_at_the_configured_time(cfg, monkeypatch):
    t = _task("run", cfg, monkeypatch)
    start = t.find("t:Triggers/t:CalendarTrigger/t:StartBoundary", NS).text
    assert start.endswith("T21:45:00")
    alert = _task("alert", cfg, monkeypatch)
    assert alert.find("t:Triggers/t:CalendarTrigger/t:StartBoundary",
                      NS).text.endswith("T07:30:00")
    assert _setting(alert, "WakeToRun") == "false"


def test_bot_task_is_always_on_and_self_healing(cfg, monkeypatch):
    t = _task("bot", cfg, monkeypatch)
    assert t.find("t:Triggers/t:LogonTrigger", NS) is not None
    rep = t.find("t:Triggers/t:TimeTrigger/t:Repetition/t:Interval", NS).text
    assert rep == "PT5M"
    assert _setting(t, "ExecutionTimeLimit") == "PT0S"
    assert t.find("t:Settings/t:RestartOnFailure", NS) is not None


def test_task_runs_as_this_user_without_admin(cfg, monkeypatch):
    t = _task("run", cfg, monkeypatch)
    p = t.find("t:Principals/t:Principal", NS)
    assert p.find("t:UserId", NS).text == "S-1-5-21-1-2-3-1001"
    assert p.find("t:LogonType", NS).text == "InteractiveToken"
    assert p.find("t:RunLevel", NS).text == "LeastPrivilege"


def test_paths_with_spaces_and_ampersands_survive(cfg, monkeypatch):
    t = _task("run", cfg, monkeypatch)
    ex = t.find("t:Actions/t:Exec", NS)
    assert ex.find("t:Command", NS).text == \
        '"' + str(Path("C:/Users/Tom & Jerry/fily/.venv/Scripts/pythonw.exe")) + '"'
    args = ex.find("t:Arguments", NS).text
    assert args.startswith("-X utf8 -m organizer.cli --log ")
    assert args.endswith(" run")
    assert ex.find("t:WorkingDirectory", NS).text == str(Path("C:/Users/Tom & Jerry/fily"))


def test_task_xml_carries_no_secrets(cfg, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSECRET")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:SECRET")
    from organizer import winsched
    for job in ("run", "alert", "bot"):
        assert "SECRET" not in winsched.build_xml(job, cfg, "S-1-5-21-9")


# ------------------------------------------------------- task status

def test_query_output_single_object_and_array():
    from organizer import winsched
    one = '{"name":"run","state":"Ready","last_result":0}'
    many = '[{"name":"run","state":"Ready"},{"name":"bot","state":"Running"}]'
    assert set(winsched.parse_query(one)) == {"run"}
    assert set(winsched.parse_query(many)) == {"run", "bot"}
    assert winsched.parse_query("") == {} and winsched.parse_query("junk") == {}


@pytest.mark.parametrize("code,expected", [
    (0, 0), (0x41303, None), (0x41301, None), (1, 1), (-2147020576, 0x800710E0),
])
def test_task_result_codes(code, expected):
    """0x41303 "has not run yet" and 0x41301 "running" aren't failures, and a
    negative 32-bit result must not look like a small number."""
    from organizer import winsched
    st = winsched.to_state("run", {"name": "run", "state": "Ready",
                                   "last_result": code,
                                   "starts": ["2026-01-01T21:45:00"]})
    assert st.last_exit == expected
    assert st.schedule == "21:45 daily"


def test_missing_task_is_reported_not_installed():
    from organizer import winsched
    st = winsched.to_state("bot", None)
    assert not st.ok and "not installed" in st.problem


def test_bot_state_running_is_ok():
    from organizer import winsched
    st = winsched.to_state("bot", {"name": "bot", "state": "Running",
                                   "last_result": 0x41301})
    assert st.running and st.ok and st.schedule == "always on"


# -------------------------------------------- localized command output

ENGLISH_POWERCFG = """Power Setting GUID: bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d  (Allow wake timers)
  Possible Setting Index: 000
  Possible Setting Friendly Name: Disable
  Possible Setting Index: 001
  Possible Setting Friendly Name: Enable
  Possible Setting Index: 002
  Possible Setting Friendly Name: Important Wake Timers Only
Current AC Power Setting Index: 0x00000001
Current DC Power Setting Index: 0x00000002
"""
RUSSIAN_POWERCFG = """GUID параметра питания: bd3b718a-0680-4d9d-8ab2-e1d2b4ac806d  (Разрешить таймеры пробуждения)
  Индекс возможного параметра: 000
Текущий индекс параметра питания от сети: 0x00000001
Текущий индекс параметра питания от батареи: 0x00000000
"""


def test_wake_timer_parsing_is_language_independent():
    assert windows.parse_rtcwake(ENGLISH_POWERCFG) == (1, 2)
    assert windows.parse_rtcwake(RUSSIAN_POWERCFG) == (1, 0)
    assert windows.parse_rtcwake("nothing useful") == (None, None)


def test_acl_check():
    """owner_only reads the ACL as structured JSON from .NET, never localized
    text: inheritance off, and every rule for this user's SID."""
    me = "S-1-5-21-1-2-3-1001"
    locked = f'{{"protected":true,"me":"{me}","rules":{{"sid":"{me}","inherited":false,"type":"Allow"}}}}'
    inherited = (f'{{"protected":false,"me":"{me}","rules":['
                 f'{{"sid":"S-1-5-18","inherited":true}},{{"sid":"{me}","inherited":true}}]}}')
    shared = (f'{{"protected":true,"me":"{me}","rules":['
              f'{{"sid":"{me}","inherited":false}},{{"sid":"S-1-5-32-545","inherited":false}}]}}')
    assert windows.parse_acl(locked)          # 1-item array unwrapped by PowerShell
    assert not windows.parse_acl(inherited)
    assert not windows.parse_acl(shared)      # "Users" can read it
    assert not windows.parse_acl("") and not windows.parse_acl("garbage")


# --------------------------------------------------------- file attributes

def _st(attrs=0, flags=0):
    return SimpleNamespace(st_file_attributes=attrs, st_flags=flags)


def test_onedrive_online_only_files_are_left_alone():
    assert windows.cloud_only(_st(windows.FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))
    assert windows.cloud_only(_st(windows.FILE_ATTRIBUTE_OFFLINE))
    assert windows.cloud_only(_st(0x20)) is None          # ARCHIVE: a normal file


def test_icloud_dataless_files_are_left_alone():
    assert macos.cloud_only(_st(flags=0x40000000))
    assert macos.cloud_only(_st(flags=0)) is None


def test_windows_hidden_and_system_files():
    assert windows.hidden("desktop.ini", _st(windows.FILE_ATTRIBUTE_HIDDEN))
    assert windows.hidden("x", _st(windows.FILE_ATTRIBUTE_SYSTEM))
    assert windows.hidden(".env", _st(0))
    assert not windows.hidden("report.pdf", _st(0x20))


def test_known_folder_aliases():
    """A config written on a Mac says ~/Movies; on Windows that's Videos."""
    assert windows.map_known_folder("~/Documents/other").endswith("other")
    assert windows.map_known_folder("~/NotAKnownFolder/x") == "~/NotAKnownFolder/x"
    assert windows.map_known_folder("/abs/path") == "/abs/path"
    assert windows.known_folder("Movies").name == "Videos"


# ------------------------------------------------------------ timeouts

NATIVE_IMPLS = [windows] if sys.platform == "win32" else [macos, windows]


@pytest.mark.parametrize("impl", NATIVE_IMPLS)
def test_timeouts_work_without_signals(impl):
    """Windows has no SIGALRM; both implementations must still give up."""
    t0 = time.monotonic()
    with pytest.raises(TimeoutError):
        impl.run_with_timeout(lambda: time.sleep(5), 0.5)
    assert time.monotonic() - t0 < 3


@pytest.mark.parametrize("impl", NATIVE_IMPLS)
def test_timeouts_pass_results_and_errors_through(impl):
    assert impl.run_with_timeout(lambda: 42, 5) == 42
    with pytest.raises(ValueError):
        impl.run_with_timeout(lambda: (_ for _ in ()).throw(ValueError("x")), 5)


# ------------------------------------------------------- process checks

def test_pid_alive_is_safe_and_correct():
    """On Windows os.kill(pid, 0) *terminates* the process; the lock must use
    a real existence check instead."""
    assert lock._alive(os.getpid())
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    assert not lock._alive(p.pid)


def test_lock_blocks_a_second_run_and_ignores_a_dead_one(tmp_path):
    """Held by another live process → Busy. Left behind by a dead one →
    taken over, so a crash can't block every future run."""
    import json
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        (tmp_path / "run.lock").write_text(
            json.dumps({"pid": other.pid, "started_human": "now"}), encoding="utf-8")
        with pytest.raises(lock.Busy):
            with lock.run_lock(tmp_path, owner="second"):
                pass
    finally:
        other.kill()
        other.wait()
    with lock.run_lock(tmp_path, owner="third"):       # stale lock taken over
        pass
    assert not (tmp_path / "run.lock").exists()


# ---------------------------------------------------------- scheduler logs

def test_log_option_captures_output_when_there_is_no_console(cfg, tmp_path,
                                                              monkeypatch):
    """pythonw.exe has no stdout; --log must catch everything, in UTF-8."""
    from organizer import cli
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    log = tmp_path / "logs" / "run.log"
    code = cli.main(["--log", str(log), "--config", str(cfg.path), "status"])
    sys.stdout.close()
    assert code == 0
    assert "no runs yet" in log.read_text(encoding="utf-8")


# -------------------------------------------------------------- health

def test_running_bot_that_cannot_reach_telegram_is_a_problem(cfg, monkeypatch):
    """A live process is not proof the bot works; the heartbeat is."""
    from organizer import bot, telegram
    from organizer.host.base import JobState
    monkeypatch.setattr(telegram, "configured", lambda: True)
    monkeypatch.setattr(health, "job_states", lambda: [
        JobState("bot", "bot", True, True, True, None, "always on")])
    monkeypatch.setattr(health, "sleep_risk", lambda: (False, "fine"))

    ok, lines = health.summary(cfg)
    assert not ok and any("hasn't reached Telegram" in l for l in lines)

    bot._beat(cfg)
    ok, lines = health.summary(cfg)
    assert not any("hasn't reached Telegram" in l for l in lines)


# ------------------------------------------------------ real Windows only

@windows_only
def test_open_file_is_detected(tmp_path):
    f = tmp_path / "busy.docx"
    f.write_bytes(b"x")
    with open(f, "rb"):
        assert windows.is_file_open(f)
    assert not windows.is_file_open(f)


@windows_only
def test_known_folders_resolve_to_real_directories():
    for name in ("Downloads", "Desktop", "Documents"):
        assert windows.known_folder(name).is_dir(), name


@windows_only
def test_secret_files_are_locked_to_this_user(tmp_path):
    f = tmp_path / ".env"
    f.write_text("K=V", encoding="utf-8")
    assert not windows.owner_only(f)
    windows.restrict_to_owner(f)
    assert windows.owner_only(f), windows.acl_report(f)
    assert f.read_text(encoding="utf-8") == "K=V"      # still readable by us


@windows_only
def test_power_settings_can_be_read():
    risky, why = windows.sleep_risk()
    assert "wake" in why


@pytest.mark.skipif(sys.platform != "win32" or not os.environ.get("FILY_CI_SCHEDULER"),
                    reason="registers real scheduled tasks; CI opts in")
def test_scheduler_end_to_end(tmp_path):
    """Register real tasks, check what Task Scheduler actually stored, run
    one, and remove them again."""
    from organizer import winsched

    root = tmp_path / "Downloads"
    root.mkdir()
    real_cfg = config.PROJECT_ROOT / "config.yaml"
    assert not real_cfg.exists(), "refusing to overwrite a real config"
    real_cfg.write_text(yaml.safe_dump({"scan_roots": [str(root)],
                                        "schedule": {"run": "21:45"}}),
                        encoding="utf-8")
    try:
        cfg = config.load(real_cfg)
        res = winsched.install(cfg, ("run", "alert"))
        assert res.ok, res.failed

        q = winsched.query()
        assert {"run", "alert"} <= set(q), q
        assert q["run"]["wake"] and q["run"]["battery_ok"] and q["run"]["catch_up"]
        assert winsched.to_state("run", q["run"]).schedule == "21:45 daily"

        assert winsched.restart("alert")
        log = winsched.log_path("alert", cfg)
        st = None
        for _ in range(40):
            time.sleep(3)
            st = winsched.to_state("alert", winsched.query().get("alert"))
            if st.last_exit is not None and not st.running:
                break
        if st is None or st.last_exit is None:
            pytest.skip("Task Scheduler did not start the task on this runner "
                        "(no interactive session); registration was verified")
        text = log.read_text(encoding="utf-8") if log.exists() else "(no log)"
        assert st.last_exit == 0, text
        assert "delivered 0 alert(s)" in text
    finally:
        winsched.uninstall()
        real_cfg.unlink(missing_ok=True)
        assert winsched.query() == {}


def test_powershell_output_has_no_byte_order_mark(monkeypatch):
    """A BOM in front of PowerShell's JSON made every result unparseable."""
    fake = SimpleNamespace(returncode=0, stdout="﻿{\"a\": 1}".encode("utf-8"),
                           stderr=b"")
    monkeypatch.setattr(windows, "_run", lambda *a, **k: fake)
    out = windows.powershell("anything").stdout
    assert out == '{"a": 1}'
    import json
    assert json.loads(out) == {"a": 1}


def test_nul_device_is_not_a_person(monkeypatch):
    """On Windows NUL claims to be a TTY; setup must not wait at a prompt."""
    class FakeStdin:
        def isatty(self):
            return False
    monkeypatch.setattr(sys, "stdin", FakeStdin())
    assert not windows.stdin_is_interactive()
    assert not macos.stdin_is_interactive()


@windows_only
def test_nul_stdin_is_detected_as_non_interactive():
    code = ("import sys; from organizer.host import windows; "
            "sys.exit(0 if not windows.stdin_is_interactive() else 3)")
    with open(os.devnull, "rb") as nul:
        r = subprocess.run([sys.executable, "-c", code], stdin=nul)
    assert r.returncode == 0, "NUL was mistaken for a console"


def test_child_powershell_does_not_inherit_ps7_module_path(monkeypatch):
    """Inherited from PowerShell 7, PSModulePath stops Windows PowerShell
    5.1 loading its own modules, so ACL and task commands silently fail."""
    seen = {}

    def fake_run(cmd, **k):
        seen.update(k.get("env") or {})
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    monkeypatch.setattr(windows, "_run", fake_run)
    windows.powershell("anything", env={"FILY_PATH": "x"})
    assert not any(k.upper() == "PSMODULEPATH" for k in seen)
    assert seen["FILY_PATH"] == "x"


def test_acl_scripts_need_no_powershell_modules():
    for script in (windows._ACL_LOCK, windows._ACL_READ):
        assert "Get-Acl" not in script and "Get-Item" not in script
