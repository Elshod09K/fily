"""macOS notifications and the deferred morning alert.

A run that fails overnight must not shout at a sleeping person. It writes
pending_alert.json instead; the morning launchd job delivers it.
"""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from . import telegram
from .config import Config

TITLE = "File Organizer"


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def delivery_log(cfg: Config) -> Path:
    return cfg.state_dir / "logs" / "telegram.log"


def telegram_push(cfg: Config, text: str,
                  buttons: list[list[dict]] | None = None) -> bool:
    """Send to the paired chat, if there is one. Never raises.

    Every attempt is logged. Sending fails soft by design — a dead network
    must not break a run — which means that without a record, "did last
    night's message arrive?" could only ever be guessed at.
    """
    if not telegram.configured():
        return False
    chat_id = telegram.load_chat_id(cfg.state_dir)
    if chat_id is None:
        _log_delivery(cfg, False, "not paired", text)
        return False
    ok = telegram.send(chat_id, text, buttons) is not None
    _log_delivery(cfg, ok, "" if ok else "send failed", text)
    return ok


def _log_delivery(cfg: Config, ok: bool, why: str, text: str) -> None:
    import re
    first = re.sub(r"<[^>]+>", "", text.strip().splitlines()[0] if text.strip() else "")
    try:
        log = delivery_log(cfg)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": time.time(), "ok": ok, "why": why,
                                 "text": first[:120]}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def last_delivery(cfg: Config) -> dict | None:
    try:
        lines = delivery_log(cfg).read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def notify(cfg: Config, message: str, subtitle: str = "", sound: bool = False,
           telegram_text: str | None = None,
           buttons: list[list[dict]] | None = None) -> bool:
    """Notify on the desktop and, when paired, in Telegram.

    Both are attempted: the desktop banner is there when sitting at the Mac,
    Telegram is there when not.
    """
    if not cfg.notify_enabled:
        return False
    telegram_push(cfg, telegram_text or f"<b>{telegram.escape(message)}</b>"
                  + (f"\n{telegram.escape(subtitle)}" if subtitle else ""),
                  buttons)
    script = (f'display notification "{_escape(message[:240])}" '
              f'with title "{TITLE}"')
    if subtitle:
        script += f' subtitle "{_escape(subtitle[:120])}"'
    if sound:
        script += ' sound name "Submarine"'
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script],
                       capture_output=True, timeout=15, check=False)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def alert_path(cfg: Config) -> Path:
    return cfg.state_dir / "pending_alert.json"


def queue_alert(cfg: Config, run_id: str, summary: str, detail: dict) -> Path:
    """Record a failure for delivery the next morning."""
    p = alert_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if p.exists():
        try:
            existing = json.loads(p.read_text()).get("alerts", [])
        except (json.JSONDecodeError, OSError):
            existing = []
    existing.append({
        "run_id": run_id, "at": time.time(),
        "when": datetime.now().isoformat(timespec="seconds"),
        "summary": summary, "detail": detail,
    })
    p.write_text(json.dumps({"alerts": existing[-20:]}, indent=2, ensure_ascii=False))
    return p


def deliver_pending(cfg: Config) -> tuple[int, str]:
    """Called by the morning job. Returns (count, message)."""
    p = alert_path(cfg)
    if not p.exists():
        return 0, "nothing pending"
    try:
        alerts = json.loads(p.read_text()).get("alerts", [])
    except (json.JSONDecodeError, OSError) as e:
        return 0, f"pending alert file unreadable: {e}"
    if not alerts:
        p.unlink(missing_ok=True)
        return 0, "nothing pending"

    latest = alerts[-1]
    if len(alerts) == 1:
        msg = latest["summary"]
        sub = f"run {latest['run_id']} — nothing was moved"
    else:
        msg = f"{len(alerts)} failed runs. Latest: {latest['summary']}"
        sub = "no files were moved on any of them"

    tried = latest.get("detail", {}).get("attempts") or []
    lines = ["⚠️ <b>Organizer could not run last night</b>", "",
             telegram.escape(latest["summary"]), ""]
    if tried:
        lines.append("<b>What it tried:</b>")
        seen: set[str] = set()
        for a in tried:
            key = f"{a.get('provider')}/{a.get('model')}"
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"• <code>{telegram.escape(key)}</code> — "
                         f"{telegram.escape(a.get('error', '?'))}")
        lines.append("")
    lines.append("<i>Your files were not touched.</i>")
    notify(cfg, msg, subtitle=sub, sound=True,
           telegram_text="\n".join(lines),
           buttons=[[{"text": "Try again now", "callback_data": "run"}]])

    archive = cfg.state_dir / "logs" / "alerts.log"
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open("a", encoding="utf-8") as fh:
        for a in alerts:
            fh.write(json.dumps(a, ensure_ascii=False) + "\n")
    p.unlink(missing_ok=True)
    return len(alerts), msg
