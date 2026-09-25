"""Minimal Telegram Bot API client.

Deliberately stdlib-only: this runs from a launchd job at odd hours, and one
less dependency is one less thing that breaks unattended. Every call fails
soft — a dead network must never take down a run or lose a file.
"""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.telegram.org/bot{token}/{method}"


def _ssl_context() -> ssl.SSLContext | None:
    """Build a context that can actually verify api.telegram.org.

    A python.org install ships no CA bundle of its own until someone runs
    "Install Certificates.command", so urllib fails to verify anything while
    curl and the httpx-based SDKs (which use certifi directly) work fine.
    That asymmetry is exactly how this went unnoticed: every other network
    call in the project succeeded. Use certifi when present, otherwise the
    system default.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


_SSL = _ssl_context()


class TelegramError(RuntimeError):
    pass


def token() -> str | None:
    return os.environ.get("TELEGRAM_BOT_TOKEN")


def configured() -> bool:
    return bool(token())


def call(method: str, params: dict | None = None, timeout: int = 30,
         raise_on_error: bool = False) -> dict | None:
    """POST to the Bot API. Returns the `result` payload, or None on failure."""
    tok = token()
    if not tok:
        if raise_on_error:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        return None
    url = API.format(token=tok, method=method)
    data = json.dumps(params or {}).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if raise_on_error:
            raise TelegramError(f"HTTP {e.code}: {body}") from e
        return None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
        if raise_on_error:
            raise TelegramError(str(e)) from e
        return None
    if not payload.get("ok"):
        if raise_on_error:
            raise TelegramError(str(payload.get("description"))[:300])
        return None
    return payload.get("result")


# --------------------------------------------------------------------- pairing

def pairing_path(state_dir: Path) -> Path:
    return state_dir / "telegram.json"


def load_chat_id(state_dir: Path) -> int | None:
    p = pairing_path(state_dir)
    if not p.exists():
        return None
    try:
        return int(json.loads(p.read_text(encoding="utf-8"))["chat_id"])
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def save_chat_id(state_dir: Path, chat_id: int, who: str = "") -> None:
    """Bind the bot to exactly one chat.

    Anyone who learns the bot's username can message it. Without this the bot
    would happily list and move this machine's files for a stranger, so the
    first /start claims it and every other chat is refused from then on.
    """
    p = pairing_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(
        {"chat_id": chat_id, "who": who, "paired_at": time.time()}, indent=2),
        encoding="utf-8")
    from . import host
    host.restrict_to_owner(p)


def clear_chat_id(state_dir: Path) -> None:
    pairing_path(state_dir).unlink(missing_ok=True)


# -------------------------------------------------------------------- messages

def escape(text: str) -> str:
    """Escape for HTML parse mode, which is far less fragile than MarkdownV2."""
    return (str(text).replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


def send(chat_id: int, text: str, buttons: list[list[dict]] | None = None,
         preview: bool = False) -> dict | None:
    params: dict = {
        "chat_id": chat_id,
        "text": text[:4096],
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": not preview},
    }
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    return call("sendMessage", params)


def edit(chat_id: int, message_id: int, text: str,
         buttons: list[list[dict]] | None = None) -> dict | None:
    params: dict = {
        "chat_id": chat_id, "message_id": message_id,
        "text": text[:4096], "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    params["reply_markup"] = {"inline_keyboard": buttons or []}
    return call("editMessageText", params)


def answer_callback(callback_id: str, text: str = "",
                    alert: bool = False) -> dict | None:
    return call("answerCallbackQuery", {
        "callback_query_id": callback_id, "text": text[:200],
        "show_alert": alert})


def get_updates(offset: int | None = None, timeout: int = 50) -> list[dict]:
    """Long poll. Raises on failure so the caller can log it.

    This deliberately does not swallow errors: an earlier version returned []
    on any failure, so a TLS problem looked exactly like "no new messages" and
    the bot spun silently for days while updates piled up server side.
    """
    params = {"timeout": timeout,
              "allowed_updates": ["message", "callback_query"]}
    if offset is not None:
        params["offset"] = offset
    return call("getUpdates", params, timeout=timeout + 15,
                raise_on_error=True) or []


def set_commands() -> dict | None:
    """Populate the in-app command menu so nothing has to be memorised."""
    return call("setMyCommands", {"commands": [
        {"command": "status", "description": "Recent runs and what is waiting"},
        {"command": "review", "description": "Work through the review queue"},
        {"command": "run", "description": "Organize now instead of waiting for tonight"},
        {"command": "report", "description": "The latest run report"},
        {"command": "undo", "description": "Reverse the most recent run"},
        {"command": "health", "description": "Check the schedule is armed"},
        {"command": "help", "description": "What this bot can do"},
    ]})


def send_document(chat_id: int, path, caption: str = "") -> dict | None:
    """Upload a file to the chat so it can be previewed on a phone.

    multipart/form-data by hand, to keep this module dependency-free.
    """
    import mimetypes
    import uuid
    from pathlib import Path as _Path

    tok = token()
    if not tok:
        return None
    path = _Path(path)
    try:
        payload = path.read_bytes()
    except OSError:
        return None

    boundary = f"----organizer{uuid.uuid4().hex}"
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def part(name: str, value: str) -> bytes:
        return (f"--{boundary}\r\nContent-Disposition: form-data; "
                f'name="{name}"\r\n\r\n{value}\r\n').encode()

    body = part("chat_id", str(chat_id))
    if caption:
        body += part("caption", caption[:1024]) + part("parse_mode", "HTML")
    body += (f"--{boundary}\r\nContent-Disposition: form-data; "
             f'name="document"; filename="{path.name}"\r\n'
             f"Content-Type: {ctype}\r\n\r\n").encode()
    body += payload + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        API.format(token=tok, method="sendDocument"), data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=180, context=_SSL) as resp:
            out = json.loads(resp.read().decode("utf-8"))
        return out.get("result") if out.get("ok") else None
    except Exception:
        return None


# ---------------------------------------------------------------- pairing code

def _code_path(state_dir: Path) -> Path:
    return state_dir / "pairing_code"


def new_pairing_code(state_dir: Path) -> str:
    """Create a one-time code that the first /start must carry.

    Without it, whoever finds the bot's username first owns it. With it, only
    the person holding the link printed by `organize setup` can pair. Telegram
    deep links deliver the code for them: t.me/<bot>?start=<code> sends
    "/start <code>" when tapped.
    """
    import secrets
    code = secrets.token_urlsafe(12)          # [A-Za-z0-9_-], deep-link safe
    p = _code_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(code, encoding="utf-8")
    from . import host
    host.restrict_to_owner(p)
    return code


def load_pairing_code(state_dir: Path) -> str | None:
    try:
        code = _code_path(state_dir).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return code or None


def clear_pairing_code(state_dir: Path) -> None:
    _code_path(state_dir).unlink(missing_ok=True)


def code_matches(expected: str, text: str) -> bool:
    import hmac
    parts = text.strip().split(maxsplit=1)
    if len(parts) != 2 or not parts[0].lower().startswith("/start"):
        return False
    return hmac.compare_digest(parts[1].strip(), expected)


def pairing_link(code: str) -> str | None:
    me = call("getMe", timeout=20)
    if not me or not me.get("username"):
        return None
    return f"https://t.me/{me['username']}?start={code}"
