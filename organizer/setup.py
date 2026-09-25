"""`organize setup` — the one-time wizard.

The promise is: clone, run once, answer a few questions, forget about it. So
every answer is checked live before it is saved — a key that is pasted wrong,
a model the key cannot use, or a folder macOS will hide from a background job
all surface here, while someone is watching, instead of as a silent failure a
week later.
"""
from __future__ import annotations

import getpass
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import yaml
from rich.console import Console

from . import config as cfgmod
from . import host, launchd, safety, telegram
from .providers import catalog

console = Console()
HOME = Path.home()

PROBE_LABEL = f"{launchd.LABEL_PREFIX}.probe"


class SetupAbort(RuntimeError):
    pass


# ------------------------------------------------------------------ prompting

class Prompter:
    def __init__(self, interactive: bool):
        self.interactive = interactive

    def ask(self, question: str, default: str = "") -> str:
        if not self.interactive:
            return default
        suffix = f" [{default}]" if default else ""
        try:
            ans = input(f"{question}{suffix}: ").strip()
        except EOFError:
            raise SetupAbort("input closed")
        return ans or default

    def secret(self, question: str, existing: str = "") -> str:
        """Hidden input, so keys stay out of the screen and the scrollback."""
        if not self.interactive:
            return existing
        hint = f" [Enter to keep {mask(existing)}]" if existing else " [Enter to skip]"
        if host.IS_WINDOWS:
            hint += " (right-click to paste; nothing shows as you type)"
        try:
            ans = getpass.getpass(f"{question}{hint}: ").strip()
        except EOFError:
            raise SetupAbort("input closed")
        return ans or existing

    def yes(self, question: str, default: bool = True) -> bool:
        if not self.interactive:
            return default
        ans = self.ask(f"{question} {'[Y/n]' if default else '[y/N]'}").lower()
        return default if not ans else ans.startswith("y")


def mask(v: str) -> str:
    if not v:
        return ""
    return v[:4] + "…" + v[-4:] if len(v) > 12 else "…" * 3


def step(n: int, title: str) -> None:
    console.print(f"\n[bold cyan]{n}. {title}[/bold cyan]")


# ------------------------------------------------------------------- .env file

def read_env(path: Path = cfgmod.ENV_FILE) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().removeprefix("export ").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def write_env(updates: dict[str, str], path: Path = cfgmod.ENV_FILE) -> None:
    """Merge keys into .env, owner-read-only, written atomically."""
    current = read_env(path)
    for k, v in updates.items():
        if v:
            current[k] = v
        else:
            current.pop(k, None)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("# Fily secrets. Owner-readable only; never commit this file.\n")
        for k, v in current.items():
            fh.write(f"{k}={v}\n")
    os.replace(tmp, path)
    host.restrict_to_owner(path)
    for k, v in updates.items():
        if v:
            os.environ[k] = v


# ---------------------------------------------------------------- the steps

def ai_keys(p: Prompter, env: dict, gemini_arg: str | None,
            nvidia_arg: str | None) -> tuple[dict, list[dict]]:
    from .providers.gemini import GeminiProvider
    from .providers.nvidia import NvidiaProvider

    step(1, "AI provider")
    console.print(
        "Fily uses an AI model only to decide what each file is about. You need "
        "at least one key; both is better, since each is the other's fallback.\n"
        "  • Gemini (recommended, generous free tier): "
        "[link]https://aistudio.google.com/apikey[/link]\n"
        "  • NVIDIA (optional, free): [link]https://build.nvidia.com[/link] → "
        "any model → Get API Key")

    for attempt in range(3):
        gkey = gemini_arg or p.secret("\nGemini API key", env.get("GEMINI_API_KEY", ""))
        nkey = nvidia_arg or p.secret("NVIDIA API key (optional)",
                                      env.get("NVIDIA_API_KEY", ""))
        if not gkey and not nkey:
            console.print("[red]At least one key is needed.[/red]")
            if not p.interactive:
                raise SetupAbort("no AI key given")
            continue

        g_ok, n_ok = [], []
        if gkey:
            g_ok = _probe("Gemini", GeminiProvider(gkey), catalog.GEMINI_CANDIDATES)
        if nkey:
            n_ok = _probe("NVIDIA", NvidiaProvider(nkey), catalog.NVIDIA_CANDIDATES)

        chain = catalog.build_chain(g_ok, n_ok)
        if chain:
            if len(chain) == 1:
                console.print("[yellow]Only one working model, so there is no "
                              "fallback if it is down. Fine to start with.[/yellow]")
            return ({"GEMINI_API_KEY": gkey if g_ok else "",
                     "NVIDIA_API_KEY": nkey if n_ok else ""}, chain)
        console.print("[red]None of those keys could run a model.[/red] Check "
                      "for a copy-paste slip and try again.")
        gemini_arg = nvidia_arg = None
        if not p.interactive:
            break
    raise SetupAbort("no working AI provider")


def _probe(name: str, provider, candidates) -> list[str]:
    with console.status(f"Checking your {name} key…"):
        ok, why = catalog.check_key(provider)
    if not ok:
        console.print(f"  [red]✗ {name}: {why}[/red]")
        return []
    with console.status(f"Testing {len(candidates)} {name} models with a real "
                        "request each…"):
        results = catalog.probe(provider, candidates)
    working = [r.model for r in results if r.ok]
    for r in results:
        if r.ok:
            console.print(f"  [green]✓[/green] {r.model}  [dim]{r.seconds:.1f}s[/dim]")
        else:
            console.print(f"  [dim]✗ {r.model} — {_why_not(r.error)}[/dim]")
    if working and len(working) < len(results):
        console.print(f"  [dim]({len(results) - len(working)} listed model(s) "
                      "didn't answer — normal; only working ones are used)[/dim]")
    if not working:
        console.print(f"  [red]{name}: the key works but no model answered.[/red]")
    return working


def _why_not(error: str) -> str:
    """One short phrase instead of a raw API error dump."""
    low = error.lower()
    if "410" in low or "retired" in low:
        return "retired by the provider"
    if "404" in low or "not found" in low or "unavailable" in low:
        return "not available to your key"
    if "timed out" in low or "timeout" in low:
        return "too slow to answer"
    if "429" in low or "rate" in low or "quota" in low:
        return "rate-limited right now"
    return error.split(":")[0][:40]


def telegram_bot(p: Prompter, env: dict, token_arg: str | None) -> tuple[str, str]:
    step(2, "Telegram bot")
    console.print(
        "Fily reports to you through your own private Telegram bot, and you "
        "review files and undo runs from there. Making one takes a minute:\n"
        "  1. In Telegram, open [bold]@BotFather[/bold] and send [bold]/newbot[/bold]\n"
        "  2. Pick any name, then a username ending in 'bot'\n"
        "  3. Copy the token it gives you (looks like 123456:ABC-DEF…)\n"
        "[dim]Skip this and you'll only get Mac notifications.[/dim]")

    for _ in range(3):
        token = token_arg or p.secret("\nBot token", env.get("TELEGRAM_BOT_TOKEN", ""))
        if not token:
            console.print("[yellow]Skipping Telegram.[/yellow]")
            return "", ""
        os.environ["TELEGRAM_BOT_TOKEN"] = token
        try:
            me = telegram.call("getMe", timeout=20, raise_on_error=True)
        except telegram.TelegramError as e:
            console.print(f"  [red]✗ Telegram rejected that token: {str(e)[:120]}"
                          "[/red]")
            token_arg = None
            if not p.interactive:
                raise SetupAbort("telegram token rejected")
            continue
        username = (me or {}).get("username", "")
        console.print(f"  [green]✓[/green] connected to @{username}")
        hook = telegram.call("getWebhookInfo", timeout=20) or {}
        if hook.get("url"):
            telegram.call("deleteWebhook", {"drop_pending_updates": False})
            console.print("  [dim]cleared an old webhook that would block Fily[/dim]")
        return token, username
    raise SetupAbort("no valid telegram token")


def folders(p: Prompter, existing: list[str], folders_arg: str | None) -> list[str]:
    step(3, "Folders to organize")
    defaults = existing or ["~/Downloads", "~/Desktop"]
    if folders_arg:
        chosen = [f.strip() for f in folders_arg.split(",") if f.strip()]
    else:
        console.print("Only loose files at the top of each folder are touched — "
                      "anything already in a subfolder is left alone. Code "
                      "projects, apps and Photos/Music libraries are always "
                      "skipped.")
        for f in defaults:
            console.print(f"  • {f}")
        chosen = list(defaults)
        if p.interactive and not p.yes("Use these?", True):
            raw = p.ask("Folders, comma-separated", ", ".join(defaults))
            chosen = [f.strip() for f in raw.split(",") if f.strip()]
        elif p.interactive:
            extra = p.ask("Add any others? (comma-separated, Enter for none)", "")
            chosen += [f.strip() for f in extra.split(",") if f.strip()]

    kept: list[str] = []
    for f in chosen:
        path = cfgmod.expand(f)
        if not path.is_dir():
            console.print(f"  [yellow]skipping {f}: not a folder[/yellow]")
            continue
        why = safety.root_rejection_reason(path)
        if why:
            console.print(f"  [yellow]skipping {f}: {why}[/yellow]")
            continue
        kept.append(f)
    if not kept:
        raise SetupAbort("no usable folders")
    return kept


def schedule(p: Prompter, existing: str, time_arg: str | None) -> str:
    step(4, "Schedule")
    default = time_arg or existing or "22:00"
    for _ in range(3):
        t = time_arg or p.ask("What time should Fily organize each day? (24h)",
                              default)
        try:
            h, m = cfgmod.parse_hhmm(t, "time")
            return f"{h:02d}:{m:02d}"
        except cfgmod.ConfigError as e:
            console.print(f"  [red]{e}[/red]")
            time_arg = None
            if not p.interactive:
                break
    raise SetupAbort("no valid time")


def write_config(roots: list[str], run_at: str, chain: list[dict]) -> Path:
    path = cfgmod.DEFAULT_CONFIG
    data: dict = {}
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        backup = path.with_suffix(".yaml.bak")
        backup.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    data["scan_roots"] = roots
    sched = data.get("schedule") if isinstance(data.get("schedule"), dict) else {}
    sched["run"] = run_at
    sched.setdefault("alert", "08:00")
    data["schedule"] = sched
    providers = data.get("providers") if isinstance(data.get("providers"), dict) else {}
    providers["chain"] = chain
    data["providers"] = providers
    if isinstance(data.get("notify"), dict):
        data["notify"].pop("alert_hour", None)     # superseded by schedule.alert

    header = ("# Your Fily settings, written by `organize setup`.\n"
              "# Anything not set here comes from config.example.yaml —\n"
              "# copy a setting over to change it.\n\n")
    path.write_text(header + yaml.safe_dump(data, sort_keys=False,
                                            allow_unicode=True, width=100), encoding="utf-8")
    return path


def probe_access_under_launchd(cfg) -> dict | None:
    """Check folder access from where it actually matters: a launchd job.

    Terminal usually has access to Downloads; the background interpreter often
    does not until it is granted Full Disk Access. Testing from this process
    would say "fine" and be wrong, so run a one-off job and read its answer.
    """
    out = cfg.state_dir / "access_probe.json"
    out.unlink(missing_ok=True)
    plist = launchd.AGENTS / f"{PROBE_LABEL}.plist"
    job = {
        "Label": PROBE_LABEL,
        "ProgramArguments": [str(launchd.entrypoint()), "probe-access",
                             "--out", str(out)],
        "WorkingDirectory": str(cfgmod.PROJECT_ROOT),
        "RunAtLoad": True,
        "StandardErrorPath": str(cfg.state_dir / "logs" / "probe.stderr.log"),
    }
    domain = f"gui/{os.getuid()}"
    subprocess.run(["/bin/launchctl", "bootout", f"{domain}/{PROBE_LABEL}"],
                   capture_output=True)
    plist.write_bytes(plistlib.dumps(job))
    subprocess.run(["/bin/launchctl", "bootstrap", domain, str(plist)],
                   capture_output=True)
    try:
        for _ in range(60):
            if out.exists():
                try:
                    return json.loads(out.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            time.sleep(0.5)
        return None
    finally:
        subprocess.run(["/bin/launchctl", "bootout", f"{domain}/{PROBE_LABEL}"],
                       capture_output=True)
        plist.unlink(missing_ok=True)


def write_probe(root: Path) -> tuple[bool, str]:
    """Can this interpreter create, rename and delete a file in `root`?

    On Windows, Controlled folder access blocks *changes* by unknown programs
    in protected folders while reading stays fine — so a read test passes and
    every move then fails. It judges by program, and Task Scheduler runs the
    same interpreter as this setup, so testing from here is representative.
    """
    import secrets as _secrets
    probe = root / f".fily-write-check-{_secrets.token_hex(4)}"
    moved = probe.with_name(probe.name + "-moved")
    try:
        probe.write_bytes(b"")
        os.rename(probe, moved)
        moved.unlink()
        return True, ""
    except OSError as e:
        for f in (probe, moved):
            try:
                f.unlink()
            except OSError:
                pass
        return False, e.strerror or type(e).__name__


def _check_access(cfg) -> tuple[list[str], str]:
    """(blocked folders, interpreter to allow)."""
    if host.IS_WINDOWS:
        blocked = [str(r) for r in cfg.scan_roots if not write_probe(r)[0]]
        return blocked, host.interpreters_needing_access()[0]
    result = probe_access_under_launchd(cfg)
    if result is None:
        return [], ""
    blocked = [r for r, v in result["roots"].items() if not v["ok"]]
    return blocked, result["interpreter"]


def folder_access(p: Prompter, cfg) -> bool:
    step(6, "Folder access")
    for attempt in range(4):
        with console.status("Checking Fily can work in your folders…"):
            blocked, interp = _check_access(cfg)
        if not blocked:
            console.print("  [green]✓[/green] Fily can work in every folder")
            return True
        what = ("Windows Security is blocking changes in" if host.IS_WINDOWS
                else "macOS is hiding")
        console.print(f"  [red]✗ {what} {len(blocked)} folder(s) from "
                      "Fily.[/red]")
        console.print("  " + host.access_fix(interp))
        if not p.interactive:
            return False
        if attempt == 0:
            try:
                host.open_url(host.ACCESS_PANE)
                host.copy_to_clipboard(interp)
                console.print("  [dim](Settings is open, and the path is on your "
                              "clipboard.)[/dim]")
            except Exception:
                pass
        if not p.yes("Done? Check again", True):
            console.print("  [yellow]Skipped. Fily will message you about this "
                          "on its first run.[/yellow]")
            return False
    return False


def pair(p: Prompter, cfg, username: str, wait: bool) -> bool:
    step(7, "Connect your phone")
    if telegram.load_chat_id(cfg.state_dir):
        console.print("  [green]✓[/green] already paired")
        return True
    code = telegram.new_pairing_code(cfg.state_dir)
    link = f"https://t.me/{username}?start={code}"
    console.print("Open this link on the phone or computer where you use "
                  "Telegram, and tap [bold]Start[/bold]:\n")
    console.print(f"    [bold link={link}]{link}[/bold link={link}]\n")
    console.print("[dim]The link contains a one-time code, so nobody else can "
                  "claim your bot even if they find it.[/dim]")
    if not wait:
        return False
    try:
        with console.status("Waiting for you to tap Start… (Ctrl-C to skip)"):
            for _ in range(300):
                if telegram.load_chat_id(cfg.state_dir):
                    console.print("  [green]✓ Paired.[/green] Check Telegram — "
                                  "Fily just said hello.")
                    return True
                time.sleep(1)
    except KeyboardInterrupt:
        pass
    console.print("  [yellow]Not yet — open the link whenever you like; the bot "
                  "is running and waiting.[/yellow]")
    return False


# ------------------------------------------------------------------- driver

def run(args) -> int:
    interactive = host.stdin_is_interactive() and not args.yes
    p = Prompter(interactive)

    console.print("\n[bold]Fily setup[/bold] — a few questions, then it runs "
                  "on its own.\n")
    console.print(
        "[dim]What it does: every day it looks at loose files in the folders "
        "you choose and moves each into a sensible subfolder.\n"
        f"What it never does: erase anything (deleting means the {host.TRASH_NAME}), touch "
        "code projects, apps or photo libraries, or send your files anywhere — "
        "only names and short text excerpts go to the AI.[/dim]")

    env = read_env()
    existing_cfg: dict = {}
    if cfgmod.DEFAULT_CONFIG.exists():
        existing_cfg = yaml.safe_load(cfgmod.DEFAULT_CONFIG.read_text(encoding="utf-8")) or {}
        console.print("\n[dim]Existing setup found — press Enter to keep any "
                      "current value.[/dim]")

    try:
        keys, chain = ai_keys(p, env, args.gemini_key, args.nvidia_key)
        token, bot_username = telegram_bot(p, env, args.telegram_token)
        roots = folders(p, existing_cfg.get("scan_roots") or [], args.folders)
        run_at = schedule(p, (existing_cfg.get("schedule") or {}).get("run", ""),
                          args.time)
    except (SetupAbort, KeyboardInterrupt) as e:
        console.print(f"\n[red]Setup stopped: {e or 'interrupted'}.[/red] "
                      "Nothing was saved. Run it again any time.")
        return 1

    step(5, "Saving and scheduling")
    write_env({**keys, "TELEGRAM_BOT_TOKEN": token})
    console.print(f"  [green]✓[/green] keys saved to {cfgmod.ENV_FILE.name} "
                  "[dim](readable only by you)[/dim]")
    path = write_config(roots, run_at, chain)
    console.print(f"  [green]✓[/green] settings saved to {path.name}")
    cfg = cfgmod.load()

    if args.skip_install:
        console.print("  [yellow]--skip-install: nothing scheduled[/yellow]")
        return 0

    sched = host.scheduler()
    jobs = sched.JOBS if token else tuple(j for j in sched.JOBS if j != "bot")
    res = sched.install(cfg, jobs)
    for line in res.failed:
        console.print(f"  [red]{line}[/red]")
    if res.failed:
        console.print("[red]Scheduling failed.[/red] "
                      + ("Run setup from the Terminal app (not an editor or "
                         "IDE terminal)." if host.IS_MAC else
                         "Try running install.cmd again."))
        return 1
    console.print(f"  [green]✓[/green] scheduled daily at {run_at}")

    access_ok = folder_access(p, cfg)
    if token:
        pair(p, cfg, bot_username, wait=not args.no_wait)

    console.print("\n[bold green]All set.[/bold green] You can close this "
                  "window.\n")
    console.print(f"Every day at [bold]{run_at}[/bold] Fily tidies "
                  f"{', '.join(roots)}"
                  + (" and reports to you on Telegram." if token else "."))
    if token:
        console.print("From Telegram: /status  /review  /run  /undo  /health")
    if not access_ok:
        console.print("[yellow]Remember the folder-access step above, or those "
                      "folders will be skipped.[/yellow]")
    console.print(f"\n[dim]{host.wake_tip(run_at, _minus)}[/dim]")
    return 0


def _minus(hhmm: str, minutes: int) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    total = (h * 60 + m - minutes) % (24 * 60)
    return f"{total // 60:02d}:{total % 60:02d}"


def probe_access(out: Path) -> int:
    """Hidden command run *by launchd* during setup: report what it can read."""
    from .scanner import root_readable
    cfg = cfgmod.load()
    roots = {}
    for r in cfg.scan_roots:
        ok, why = root_readable(r)
        roots[str(r)] = {"ok": ok, "why": why}
    out.write_text(json.dumps({
        "interpreter": str(Path(sys.executable).resolve()),
        "roots": roots,
    }), encoding="utf-8")
    return 0
