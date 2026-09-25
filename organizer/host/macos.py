"""macOS implementations of everything that depends on the operating system."""
from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

NAME = "macOS"
FILE_MANAGER = "Finder"
TRASH_NAME = "Trash"
RESTORE_HINT = "Finder → right-click → Put Back"
CLI = ".venv/bin/organize"
TRASH_DIR: Path | None = Path.home() / ".Trash"

HOME = Path.home()

# APFS "dataless" files: iCloud Drive content that exists only in the cloud.
# Reading one downloads it, so hashing or extracting text from every such file
# would quietly pull a whole iCloud Desktop down to disk.
_SF_DATALESS = 0x40000000


# ------------------------------------------------------------------- folders

def protected_roots() -> tuple[Path, ...]:
    """Never scanned, never written. Listed in resolved form: config paths are
    resolved first, and /etc and /var are symlinks into /private. A blanket
    /private or /var is deliberately absent — it would also cover
    /private/var/folders, where every per-user temp directory lives."""
    return (
        HOME / "Library", HOME / ".Trash", HOME / "Applications",
        Path("/System"), Path("/Library"), Path("/Applications"),
        Path("/usr"), Path("/bin"), Path("/sbin"), Path("/cores"), Path("/opt"),
        Path("/etc"), Path("/private/etc"),
        Path("/var/db"), Path("/private/var/db"),
        Path("/var/root"), Path("/private/var/root"),
        Path("/var/vm"), Path("/private/var/vm"),
        Path("/var/log"), Path("/private/var/log"),
    )


def library_roots() -> tuple[Path, ...]:
    """Destination-only: these hold Apple-managed library bundles."""
    return (HOME / "Pictures", HOME / "Movies", HOME / "Music")


def known_folder(name: str) -> Path | None:
    return HOME / name


def map_known_folder(path: str) -> str:
    return path


# --------------------------------------------------------------------- files

def hidden(name: str, st: os.stat_result) -> bool:
    return name.startswith(".")


def cloud_only(st: os.stat_result) -> str | None:
    if getattr(st, "st_flags", 0) & _SF_DATALESS:
        return "iCloud-only (not downloaded) — left alone so it isn't fetched"
    return None


def is_link_dir(path: Path) -> bool:
    return path.is_symlink()


def is_file_open(path: Path) -> bool:
    """True if another process holds the file open. On a timeout assume it
    is, so an active writer is never raced; a missing lsof assumes not."""
    try:
        r = subprocess.run(["/usr/sbin/lsof", "-t", "--", str(path)],
                           capture_output=True, timeout=10, check=False)
        return bool(r.stdout.strip())
    except subprocess.TimeoutExpired:
        return True
    except (FileNotFoundError, OSError):
        return False


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)          # signal 0: existence check, sends nothing
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def run_with_timeout(fn, seconds: float):
    """Call fn(), raising TimeoutError after `seconds`.

    SIGALRM interrupts even C-level work in the main thread; elsewhere signals
    are unavailable, so fall back to a watchdog thread.
    """
    if threading.current_thread() is not threading.main_thread():
        return _thread_timeout(fn, seconds)

    def _raise(signum, frame):
        raise TimeoutError(f"took longer than {seconds:g}s")

    previous = signal.signal(signal.SIGALRM, _raise)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        return fn()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _thread_timeout(fn, seconds: float):
    box: dict = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as e:           # re-raised in the caller
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

def restrict_to_owner(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def owner_only(path: Path) -> bool:
    return (path.stat().st_mode & 0o077) == 0


# ------------------------------------------------------------------ desktop

def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify_desktop(title: str, message: str, subtitle: str = "",
                   sound: bool = False) -> bool:
    script = f'display notification "{_escape(message[:240])}" with title "{_escape(title)}"'
    if subtitle:
        script += f' subtitle "{_escape(subtitle[:120])}"'
    if sound:
        script += ' sound name "Submarine"'
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script],
                       capture_output=True, timeout=15)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def reveal(path: Path) -> None:
    subprocess.run(["/usr/bin/open", "-R", str(path)], capture_output=True,
                   timeout=15, check=False)


def open_url(url: str) -> None:
    subprocess.run(["/usr/bin/open", url], capture_output=True, timeout=15)


def copy_to_clipboard(text: str) -> None:
    subprocess.run(["/usr/bin/pbcopy"], input=text.encode("utf-8"),
                   capture_output=True, timeout=10)


def stdin_is_interactive() -> bool:
    import sys
    try:
        return bool(sys.stdin) and sys.stdin.isatty()
    except (ValueError, AttributeError):
        return False


def no_window() -> dict:
    return {}


# -------------------------------------------------------------------- power

def sleep_risk() -> tuple[bool, str]:
    """Will the daily job fire on time, or be deferred by sleep?

    launchd never runs a calendar job while the Mac is asleep; it runs it on
    the next wake instead. So the real question is whether this Mac sleeps.
    """
    try:
        r = subprocess.run(["/usr/bin/pmset", "-g", "custom"],
                           capture_output=True, text=True, timeout=10, encoding="utf-8")
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
                           capture_output=True, text=True, timeout=10, encoding="utf-8")
    except (OSError, subprocess.TimeoutExpired):
        return None
    # Only the "Repeating power events" section counts; the other list is
    # mostly Apple's own invisible maintenance alarms.
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


def wake_tip(run_at: str, minus) -> str:
    return ("Laptop tip: macOS runs nothing while asleep; a missed run happens "
            "when you next open the lid. To run on time even when asleep:\n"
            f"  sudo pmset repeat wakeorpoweron MTWRFSU {minus(run_at, 3)}:00")


# ------------------------------------------------------------- access help

def access_fix(interpreter: str) -> str:
    return ("One-time fix: [bold]System Settings → Privacy & Security → Full "
            "Disk Access[/bold]\n  → click [bold]+[/bold] → press [bold]⌘⇧G[/bold] "
            "→ paste this path → Open → make sure it's switched on:\n"
            f"\n    [bold]{interpreter}[/bold]\n")


def access_fix_html(interpreter: str, esc) -> list[str]:
    return ["<b>Fix, once:</b> System Settings → Privacy &amp; Security → "
            "<b>Full Disk Access</b> → <b>+</b> → press ⌘⇧G and paste:",
            f"<code>{esc(interpreter)}</code>"]


ACCESS_PANE = "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"


def interpreters_needing_access() -> list[str]:
    import sys
    return [str(Path(sys.executable).resolve())]
