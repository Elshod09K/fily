"""Windows implementations of everything that depends on the operating system.

Every Win32 call is made lazily inside a function, so this module imports on
any OS. That keeps the parsing and XML-building logic unit-testable anywhere;
the real API calls are exercised by the Windows CI job.
"""
from __future__ import annotations

import base64
import csv
import io
import os
import re
import subprocess
import sys
import threading
from pathlib import Path

NAME = "Windows"
FILE_MANAGER = "File Explorer"
TRASH_NAME = "Recycle Bin"
RESTORE_HINT = "Recycle Bin → right-click → Restore"
CLI = r".venv\Scripts\organize"
TRASH_DIR: Path | None = None

HOME = Path.home()

# File attribute bits (winnt.h)
FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_SYSTEM = 0x4
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003      # a junction

CREATE_NO_WINDOW = 0x08000000

# Known folders can be relocated — on most Windows 11 laptops OneDrive moves
# Desktop, Documents and Pictures under ~/OneDrive — so ~/Desktop is often not
# the real Desktop. Ask the shell instead of guessing.
KNOWN_FOLDERS = {
    "Downloads": "374DE290-123F-4565-9164-39C4925E467B",
    "Desktop": "B4BFCC3A-DB2C-424C-B029-7FE99A87C641",
    "Documents": "FDD39AD0-238F-46AF-ADB4-6C85480369C7",
    "Pictures": "33E28130-4E1E-4676-835A-98395C3BC3BB",
    "Videos": "18989B1D-99B5-455B-841C-AB7C74E4DDFC",
    "Music": "4BD8D571-6D19-48D3-BE97-422220080E43",
}
# A config written on a Mac says ~/Movies; Windows calls it Videos.
ALIASES = {"Movies": "Videos"}


def stdin_is_interactive() -> bool:
    """Is a person at a console? Not the same as isatty() on Windows: the NUL
    device reports itself as a TTY, so a script feeding setup from NUL would
    be treated as interactive and hang at a password prompt, since getpass
    reads the console directly rather than stdin."""
    import sys as _sys
    try:
        if not _sys.stdin or not _sys.stdin.isatty():
            return False
        if _sys.platform != "win32":
            return True
        import ctypes
        import msvcrt
        from ctypes import wintypes
        handle = msvcrt.get_osfhandle(_sys.stdin.fileno())
        mode = wintypes.DWORD()
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (OSError, ValueError, AttributeError):
        return False


def no_window() -> dict:
    """Keep console windows from flashing when running under pythonw."""
    return {"creationflags": CREATE_NO_WINDOW}


def _run(cmd, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("capture_output", True)
    kw.setdefault("timeout", 30)
    return subprocess.run(cmd, **no_window(), **kw)


def clean_env(extra: dict | None = None) -> dict:
    """The environment for a child powershell.exe, minus PSModulePath.

    Started from inside PowerShell 7 (Windows Terminal's usual default, and
    GitHub's runners), Windows PowerShell 5.1 inherits PowerShell 7's module
    path and then can't load its own built-in modules — Get-Acl, the
    scheduled-task cmdlets — so the work silently doesn't happen. Without the
    variable, 5.1 rebuilds its default.
    """
    env = {k: v for k, v in os.environ.items() if k.upper() != "PSMODULEPATH"}
    env.update(extra or {})
    return env


def powershell(script: str, env: dict | None = None,
               timeout: int = 60) -> subprocess.CompletedProcess:
    """Run a PowerShell script, passed encoded so quoting can never break it.

    Output is forced to UTF-8 so non-English Windows installs and non-ASCII
    file names come back intact.
    """
    # UTF8Encoding($false): no byte-order mark. [Text.Encoding]::UTF8 would
    # prefix every result with U+FEFF, which JSON parsing rejects — silently
    # turning "locked" into "not locked" and "installed" into "missing".
    full = ("$ProgressPreference = 'SilentlyContinue'\n"
            "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false\n"
            + script)
    encoded = base64.b64encode(full.encode("utf-16-le")).decode("ascii")
    r = _run(["powershell.exe", "-NoProfile", "-NonInteractive",
              "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
             env=clean_env(env), timeout=timeout)
    # utf-8-sig strips a BOM if one still gets through.
    r.stdout = (r.stdout or b"").decode("utf-8-sig", "replace")
    r.stderr = (r.stderr or b"").decode("utf-8-sig", "replace")
    return r


# ------------------------------------------------------------------- folders

def known_folder(name: str) -> Path | None:
    name = ALIASES.get(name, name)
    guid = KNOWN_FOLDERS.get(name)
    if guid is None or sys.platform != "win32":
        return HOME / name
    import ctypes
    import uuid
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                    ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    u = uuid.UUID(guid)
    g = GUID(u.time_low, u.time_mid, u.time_hi_version,
             (ctypes.c_ubyte * 8)(*u.bytes[8:]))
    out = ctypes.c_wchar_p()
    shell32 = ctypes.windll.shell32
    shell32.SHGetKnownFolderPath.argtypes = [
        ctypes.POINTER(GUID), wintypes.DWORD, wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p)]
    hr = shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(out))
    try:
        return Path(out.value) if hr == 0 and out.value else HOME / name
    finally:
        ctypes.windll.ole32.CoTaskMemFree(out)


def map_known_folder(path: str) -> str:
    """Turn ~/Desktop into wherever Windows really keeps the Desktop."""
    m = re.match(r"^~[\\/]([^\\/]+)(.*)$", path)
    if not m:
        return path
    name, rest = m.group(1), m.group(2)
    if ALIASES.get(name, name) not in KNOWN_FOLDERS:
        return path
    real = known_folder(name)
    return str(real) + rest if real else path


def _appdata_roots() -> list[Path]:
    """Application data, minus the temp folder.

    Windows keeps TEMP inside AppData\\Local. Protecting AppData wholesale
    would make every temporary folder off-limits (the Windows version of
    /private/var/folders on a Mac), so protect each application's folder
    under Local individually and leave Temp alone.
    """
    import tempfile
    appdata = HOME / "AppData"
    roots = [appdata / "Roaming", appdata / "LocalLow"]
    local = appdata / "Local"
    try:
        temp = Path(tempfile.gettempdir()).resolve()
        for child in local.iterdir():
            c = child.resolve()
            if c == temp or c in temp.parents:
                continue
            roots.append(child)
    except OSError:
        roots.append(local)
    return roots


def protected_roots() -> tuple[Path, ...]:
    drive = Path(os.environ.get("SystemDrive", "C:") + "\\")
    env = os.environ.get
    roots = [
        Path(env("WINDIR", str(drive / "Windows"))),
        Path(env("ProgramFiles", str(drive / "Program Files"))),
        Path(env("ProgramFiles(x86)", str(drive / "Program Files (x86)"))),
        Path(env("ProgramData", str(drive / "ProgramData"))),
        *_appdata_roots(),
        drive / "$Recycle.Bin",
        drive / "System Volume Information",
        drive / "Recovery",
        drive / "PerfLogs",
    ]
    return tuple(dict.fromkeys(roots))


def library_roots() -> tuple[Path, ...]:
    """Destination-only, matching the Mac behaviour: loose media is routed
    into these, so scanning them would feed Fily its own output."""
    return tuple(p for p in (known_folder("Pictures"), known_folder("Videos"),
                             known_folder("Music")) if p)


# --------------------------------------------------------------------- files

def _attrs(st: os.stat_result) -> int:
    return getattr(st, "st_file_attributes", 0) or 0


def hidden(name: str, st: os.stat_result) -> bool:
    return name.startswith(".") or bool(
        _attrs(st) & (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM))


def cloud_only(st: os.stat_result) -> str | None:
    """OneDrive "online-only" placeholders look like ordinary files, but any
    read downloads them. Hashing or extracting text from every one would pull
    a whole OneDrive Desktop down to disk, so they are left alone."""
    if _attrs(st) & (FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
                     | FILE_ATTRIBUTE_RECALL_ON_OPEN | FILE_ATTRIBUTE_OFFLINE):
        return "OneDrive online-only (not downloaded) — left alone so it isn't fetched"
    return None


def is_link_dir(path: Path) -> bool:
    """Symlinks and junctions. Not all reparse points: OneDrive marks its own
    ordinary folders with one, and those must still count as folders."""
    if path.is_symlink():
        return True
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return isjunction(path)
    try:
        return getattr(os.lstat(path), "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT
    except OSError:
        return False


def is_file_open(path: Path) -> bool:
    """Try to open the file exclusively; a sharing violation means someone
    else has it open. Read access is required — Windows skips sharing checks
    entirely for attribute-only opens — and FILE_FLAG_OPEN_NO_RECALL keeps a
    cloud file from being downloaded by the check itself."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD,
                                wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    GENERIC_READ, OPEN_EXISTING = 0x80000000, 3
    FILE_FLAG_OPEN_NO_RECALL = 0x00100000
    INVALID = wintypes.HANDLE(-1).value

    h = k32.CreateFileW(str(path), GENERIC_READ, 0, None, OPEN_EXISTING,
                        FILE_FLAG_OPEN_NO_RECALL, None)
    if h == INVALID or h is None:
        err = ctypes.get_last_error()
        return err in (32, 33)            # SHARING_VIOLATION, LOCK_VIOLATION
    k32.CloseHandle(h)
    return False


def pid_alive(pid: int) -> bool:
    """Never os.kill(pid, 0) here: on Windows that *terminates* the process."""
    if sys.platform != "win32":
        return False
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    PROCESS_QUERY_LIMITED_INFORMATION, STILL_ACTIVE = 0x1000, 259

    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ctypes.get_last_error() == 5          # access denied: it exists
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return True
        return code.value == STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def run_with_timeout(fn, seconds: float):
    """Windows has no SIGALRM, so run fn on a daemon thread and stop waiting.

    A daemon thread, not a thread pool: pool threads are joined at exit, so a
    PDF parser stuck forever would keep the whole run from finishing.
    """
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:
            box["error"] = e

    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise TimeoutError(f"took longer than {seconds:g}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


# ---------------------------------------------------------------- privacy

_ME = "[System.Security.Principal.WindowsIdentity]::GetCurrent().User"


def current_user_sid() -> str | None:
    """The SID, not the name: names are localized and can contain spaces.
    Asked of .NET rather than parsed from `whoami`, whose text output
    depends on the console's code page."""
    try:
        r = powershell(f"{_ME}.Value", timeout=30)
        sid = (r.stdout or "").strip().splitlines()[-1].strip() if r.stdout.strip() else ""
        if sid.startswith("S-"):
            return sid
    except (OSError, subprocess.TimeoutExpired, IndexError):
        pass
    try:
        r = _run(["whoami", "/user", "/fo", "csv", "/nh"], timeout=15)
        text = (r.stdout or b"").decode("utf-8", "replace")
        row = next(csv.reader(io.StringIO(text.strip())), None)
        return row[1] if row and len(row) > 1 and row[1].startswith("S-") else None
    except (OSError, subprocess.TimeoutExpired, StopIteration):
        return None


# chmod 600, Windows edition: a fresh security descriptor holding one rule —
# full control for this user — with inheritance switched off. Built as a new
# FileSecurity (DACL only) so it never tries to rewrite the file's owner.
_ACL_LOCK = r"""
$me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$sec = New-Object System.Security.AccessControl.FileSecurity
$sec.SetAccessRuleProtection($true, $false)
$sec.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule($me, 'FullControl', 'Allow')))
[System.IO.File]::SetAccessControl($env:FILY_PATH, $sec)
"""

_ACL_READ = r"""
$acl = [System.IO.File]::GetAccessControl($env:FILY_PATH)
$rules = @($acl.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier]) | ForEach-Object {
  [pscustomobject]@{ sid = $_.IdentityReference.Value; inherited = [bool]$_.IsInherited; type = $_.AccessControlType.ToString() }
})
ConvertTo-Json -Compress -Depth 4 -InputObject ([pscustomobject]@{
  protected = [bool]$acl.AreAccessRulesProtected
  me = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
  rules = $rules
})
"""


def restrict_to_owner(path: Path) -> None:
    """A copy cloned to C:\\fily would otherwise inherit rules that let other
    accounts on the PC read the keys."""
    powershell(_ACL_LOCK, env={"FILY_PATH": str(path)}, timeout=30)


def parse_acl(text: str) -> bool:
    """True when inheritance is off and every rule belongs to this user."""
    import json
    try:
        d = json.loads((text or "").strip() or "{}")
    except json.JSONDecodeError:
        return False
    rules = d.get("rules") or []
    if isinstance(rules, dict):              # PowerShell unwraps 1-item arrays
        rules = [rules]
    me = d.get("me")
    return bool(d.get("protected")) and bool(rules) and bool(me) and all(
        r.get("sid") == me and not r.get("inherited") for r in rules)


def acl_report(path: Path) -> str:
    """The raw ACL JSON, for diagnostics."""
    r = powershell(_ACL_READ, env={"FILY_PATH": str(path)}, timeout=30)
    return r.stdout + (f"\n[stderr] {r.stderr.strip()}" if r.stderr.strip() else "")


def owner_only(path: Path) -> bool:
    return parse_acl(powershell(_ACL_READ, env={"FILY_PATH": str(path)},
                                timeout=30).stdout)


# ------------------------------------------------------------------ desktop

_TOAST = r"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$t = [Security.SecurityElement]::Escape($env:FILY_TITLE)
$m = [Security.SecurityElement]::Escape($env:FILY_MSG)
$s = [Security.SecurityElement]::Escape($env:FILY_SUB)
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$t</text><text>$m</text><text>$s</text></binding></visual></toast>")
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show((New-Object Windows.UI.Notifications.ToastNotification $xml))
"""


def notify_desktop(title: str, message: str, subtitle: str = "",
                   sound: bool = False) -> bool:
    """A Windows toast. Text travels in environment variables, never pasted
    into the script: messages contain file names, and a file name must not be
    able to become PowerShell code."""
    try:
        r = powershell(_TOAST, env={"FILY_TITLE": title[:120],
                                    "FILY_MSG": message[:240],
                                    "FILY_SUB": subtitle[:120]}, timeout=20)
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def reveal(path: Path) -> None:
    # explorer wants exactly /select,"path". A command string rather than a
    # list, because list quoting would wrap the whole argument. Windows paths
    # cannot contain a double quote, so this cannot be broken out of.
    subprocess.run(f'explorer.exe /select,"{path}"', **no_window(),
                   capture_output=True, timeout=15, check=False)


def open_url(url: str) -> None:
    os.startfile(url)                                   # noqa: S606


def copy_to_clipboard(text: str) -> None:
    powershell("Set-Clipboard -Value $env:FILY_CLIP", env={"FILY_CLIP": text},
               timeout=15)


# -------------------------------------------------------------------- power

def parse_rtcwake(output: str) -> tuple[int | None, int | None]:
    """(AC, DC) "Allow wake timers" from `powercfg /q ... RTCWAKE`.

    The labels are localized, so match the two "…: 0x0000000N" values, which
    powercfg always prints AC first, then DC.
    """
    values = re.findall(r":\s*0x([0-9a-fA-F]{8})\s*$", output, flags=re.M)
    if len(values) < 2:
        return None, None
    return int(values[-2], 16), int(values[-1], 16)


_WAKE = {0: "disabled", 1: "enabled", 2: "important only"}


def sleep_risk() -> tuple[bool, str]:
    """The run task asks Windows to wake the PC. Whether it's allowed to is
    the power plan's "Allow wake timers" setting — often "important only" on
    battery, which excludes ordinary scheduled tasks."""
    try:
        r = _run(["powercfg", "/q", "SCHEME_CURRENT", "SUB_SLEEP", "RTCWAKE"],
                 timeout=15)
        text = (r.stdout or b"").decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired):
        return True, "could not read power settings"
    ac, dc = parse_rtcwake(text)
    if ac is None:
        return True, ("could not read wake-timer settings; a run due while "
                      "asleep happens at the next wake")
    bits = [f"wake timers: plugged in {_WAKE.get(ac, ac)}, on battery "
            f"{_WAKE.get(dc, dc)}"]
    if ac == 1 and dc == 1:
        bits.append("the PC wakes itself to run on time")
        return False, "; ".join(bits)
    bits.append("when it can't wake, a missed run happens at the next wake")
    return True, "; ".join(bits)


def wake_tip(run_at: str, minus) -> str:
    return ("Laptop tip: Fily asks Windows to wake the PC for its run. On "
            "battery, Windows often allows only 'important' wake timers. To "
            "allow it (optional):\n"
            "  powercfg /setdcvalueindex SCHEME_CURRENT SUB_SLEEP RTCWAKE 1\n"
            "  powercfg /setactive SCHEME_CURRENT")


# ------------------------------------------------------------- access help

def interpreters_needing_access() -> list[str]:
    """The *base* interpreters. A venv's python.exe is only a redirector that
    launches the real one, and it's the real one Windows Security judges."""
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve().parent
    return [str(base / "python.exe"), str(base / "pythonw.exe")]


def access_fix(interpreter: str) -> str:
    paths = "\n".join(f"    [bold]{p}[/bold]" for p in interpreters_needing_access())
    return ("Windows Security's [bold]Controlled folder access[/bold] is "
            "blocking changes. One-time fix:\n  Windows Security → Virus & "
            "threat protection → Ransomware protection → [bold]Allow an app "
            "through Controlled folder access[/bold] → Add an allowed app → "
            "Browse all apps → add both:\n\n" + paths + "\n")


def access_fix_html(interpreter: str, esc) -> list[str]:
    return (["<b>Fix, once:</b> Windows Security → Virus &amp; threat "
             "protection → Ransomware protection → <b>Allow an app through "
             "Controlled folder access</b> → add both:"]
            + [f"<code>{esc(p)}</code>" for p in interpreters_needing_access()])


ACCESS_PANE = "windowsdefender://ransomwareprotection"
