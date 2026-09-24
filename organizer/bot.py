"""Telegram front end.

Runs as its own always-on launchd job, long-polling for commands. It is a
*view* over the same state the CLI uses — the nightly job needs none of this
to work, and the bot being down never stops files being organized.

Security: the bot is bound to a single chat on first /start. Anyone can find a
bot by username, and this one can list and move a real home directory, so every
other chat is refused.
"""
from __future__ import annotations

import html
import json
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import applier, config as cfgmod, health, journal, planner, safety, scanner, telegram, trash
from .telegram import escape

POLL_TIMEOUT = 50
MAX_LIST = 12

# Telegram accepts documents up to 50 MB from a bot; stay under it.
MAX_UPLOAD_BYTES = 45 * 1024 * 1024
SNIPPET_ON_CARD = 220


def _source_fingerprint() -> float:
    """Newest mtime across the package's source files."""
    pkg = Path(__file__).resolve().parent
    try:
        return max(p.stat().st_mtime for p in pkg.rglob("*.py"))
    except (OSError, ValueError):
        return 0.0


# --------------------------------------------------------------- shared state

class Session:
    """What the bot is in the middle of, per chat."""

    def __init__(self):
        self.queue: list[dict] = []
        self.index: int = 0
        self.awaiting_folder_for: int | None = None
        self.message_id: int | None = None
        self.undo_target: str | None = None

    def current(self) -> dict | None:
        while self.index < len(self.queue):
            item = self.queue[self.index]
            if Path(item["path"]).exists():
                return item
            self.index += 1
        return None


def load_queue(cfg) -> list[dict]:
    p = cfg.state_dir / "review_queue.json"
    if not p.exists():
        return []
    try:
        items = json.loads(p.read_text()).get("items", [])
    except (json.JSONDecodeError, OSError):
        return []
    return [i for i in items if Path(i["path"]).exists()]


def save_queue(cfg, items: list[dict]) -> None:
    p = cfg.state_dir / "review_queue.json"
    try:
        existing = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        existing = {}
    existing["items"] = items
    p.write_text(json.dumps(existing, indent=2, ensure_ascii=False),
                 encoding="utf-8")


def suggested_folder(item: dict) -> str:
    s = item.get("suggestion") or ""
    if not s:
        return ""
    p = Path(s)
    if p.is_absolute():
        root = Path(item["root"])
        try:
            return str(p.parent.relative_to(root))
        except ValueError:
            return p.parent.name
    return s


# ------------------------------------------------------------------- rendering

def fmt_status(cfg) -> str:
    cache = journal.Cache(cfg)
    runs = cache.recent_runs(5)
    cache.close()

    lines = ["<b>File Organizer</b>", ""]
    if runs:
        lines.append("<b>Recent runs</b>")
        for r in runs:
            when = (datetime.fromtimestamp(r["started_at"]).strftime("%d %b %H:%M")
                    if r["started_at"] else "—")
            icon = {"ok": "✅", "dry-run": "🔍", "failed": "❌",
                    "timeout": "⏱"}.get(r["status"], "•")
            lines.append(f"{icon} {escape(when)} — moved <b>{r['moved'] or 0}</b>, "
                         f"review <b>{r['queued'] or 0}</b>")
    else:
        lines.append("<i>No runs yet.</i>")

    n = len(load_queue(cfg))
    lines += ["", f"📋 <b>{n}</b> file(s) waiting for you"
              if n else "📋 Nothing waiting for review"]

    pending = cfg.state_dir / "pending_alert.json"
    if pending.exists():
        lines.append("⚠️ There is an undelivered failure alert")

    lines += ["", f"🕙 Next automatic run: <b>{cfg.schedule.run_at}</b>"]

    ok, _ = health.summary(cfg)
    lines.append("💚 Everything is armed" if ok
                 else "🩺 Something needs attention — /health")
    return "\n".join(lines)


def fmt_health(cfg) -> str:
    ok, rows = health.summary(cfg)
    icon = {"ok": "✅", "PROBLEM": "❌", "warn": "⚠️", "—": "▫️"}
    lines = ["<b>Health check</b>", ""]
    for row in rows:
        mark, _, rest = row.partition(" ")
        lines.append(f"{icon.get(mark, '•')} {escape(rest.strip())}")
    lines += ["", "💚 <b>All good.</b>" if ok else
              "❗️ <b>Not fully working.</b> In Terminal:\n"
              "<code>cd ~/Agents/file-organizer && .venv/bin/organize install</code>"]
    return "\n".join(lines)


def fmt_review_card(item: dict, index: int, total: int) -> tuple[str, list[list[dict]]]:
    name = Path(item["path"]).name
    folder = suggested_folder(item)
    root = Path(item["root"]).name

    lines = [f"<b>Review {index + 1} of {total}</b>", "",
             f"📄 <code>{escape(name)}</code>",
             f"📁 in <i>{escape(root)}/</i>"]

    size = item.get("size")
    if size:
        lines[-1] += f"  ·  {_human_size(size)}"
    lines += ["", f"<i>{escape(item.get('why', ''))}</i>"]

    # An excerpt answers "what even is this?" far better than the filename,
    # and it was already extracted during classification.
    excerpt = (item.get("snippet") or "").strip()
    if excerpt:
        trimmed = excerpt[:SNIPPET_ON_CARD].replace("\n", " ")
        if len(excerpt) > SNIPPET_ON_CARD:
            trimmed += "…"
        lines += ["", f"<blockquote>{escape(trimmed)}</blockquote>"]
    elif item.get("snippet_note"):
        lines += ["", f"<i>{escape(item['snippet_note'])}</i>"]
    # The reason text usually already states the confidence; don't say it twice.
    if item.get("confidence") and "confidence" not in item.get("why", "").lower():
        lines.append(f"<i>confidence {item['confidence']:.2f}</i>")
    if folder:
        lines += ["", f"Suggested: <b>{escape(folder)}/</b>"]

    buttons: list[list[dict]] = []
    if folder:
        buttons.append([{"text": f"✅ Move to {folder[:24]}",
                         "callback_data": f"rv:a:{index}"}])
    buttons.append([
        {"text": "👁 Show in Finder", "callback_data": f"rv:f:{index}"},
        {"text": "📄 Send me it", "callback_data": f"rv:p:{index}"},
    ])
    buttons.append([
        {"text": "✏️ Different folder", "callback_data": f"rv:e:{index}"},
        {"text": "⏭ Skip", "callback_data": f"rv:s:{index}"},
    ])
    buttons.append([{"text": "🗑 Delete", "callback_data": f"rv:d:{index}"},
                    {"text": "✖️ Stop", "callback_data": "rv:q"}])
    return "\n".join(lines), buttons


def _human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}"
        n /= 1024.0
    return f"{n:.0f} GB"


def reveal_in_finder(path: Path) -> tuple[bool, str]:
    """Select the file in Finder on the Mac running the bot.

    `file://` links are not clickable in Telegram, but the bot is on the same
    machine as the files, so it can just ask Finder to reveal it.
    """
    if not path.exists():
        return False, "that file is no longer there"
    try:
        subprocess.run(["/usr/bin/open", "-R", str(path)],
                       capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"could not reach Finder: {type(e).__name__}"
    return True, "revealed in Finder"


def send_file_for_preview(chat_id: int, item: dict) -> tuple[bool, str]:
    """Upload the file so it can be read on a phone.

    This puts a copy on Telegram's servers, so it only ever happens when the
    button is tapped for that specific file — never automatically.
    """
    path = Path(item["path"])
    if not path.exists():
        return False, "that file is no longer there"
    size = path.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        return False, f"too big to send ({_human_size(size)})"
    sent = telegram.send_document(
        chat_id, path,
        caption=f"<code>{escape(path.name)}</code>\n"
                f"<i>from {escape(Path(item['root']).name)}/</i>")
    if sent is None:
        return False, "upload failed"
    return True, "sent"


def fmt_delete_confirm(item: dict, index: int) -> tuple[str, list[list[dict]]]:
    name = Path(item["path"]).name
    try:
        size = Path(item["path"]).stat().st_size / 1048576
        size_s = f"{size:.1f} MB"
    except OSError:
        size_s = "unknown size"
    text = ("🗑 <b>Delete this file?</b>\n\n"
            f"📄 <code>{escape(name)}</code>\n"
            f"{escape(size_s)}\n\n"
            "<i>It goes to the macOS Trash. Recover it any time from "
            "Finder \u2192 right-click \u2192 Put Back. It is not erased.</i>")
    return text, [[{"text": "🗑 Yes, delete", "callback_data": f"rv:D:{index}"},
                   {"text": "Cancel", "callback_data": f"rv:c:{index}"}]]


def _folder_list(cfg) -> str:
    home = Path.home()
    names = []
    for r in cfg.scan_roots:
        try:
            names.append("~/" + str(r.relative_to(home)))
        except ValueError:
            names.append(str(r))
    return ", ".join(names)


def fmt_help(cfg) -> str:
    return HELP.format(folders=escape(_folder_list(cfg)),
                       run_at=cfg.schedule.run_at,
                       alert_at=cfg.schedule.alert_at)


HELP = """<b>Fily — your file organizer</b>

I tidy loose files in {folders} every day at <b>{run_at}</b>, and tell you \
what I did.

<b>/status</b> — recent runs and what is waiting
<b>/review</b> — go through the files I wasn't sure about
<b>/run</b> — organize now, don't wait for tonight
<b>/report</b> — the full report from the last run
<b>/undo</b> — put everything from the last run back
<b>/health</b> — check the schedule is actually armed

While reviewing, <b>👁 Show in Finder</b> highlights the file on the Mac, and \
<b>📄 Send me it</b> uploads it here so you can read it on your phone.

Duplicate copies of identical files go to the <b>Trash</b> automatically — \
the oldest copy is always kept, and I re-check both files are still identical \
right before deleting. You can also tap 🗑 on any file while reviewing.

Nothing is ever erased outright: deleting means the macOS Trash. Get anything \
back with Finder → right-click → <b>Put Back</b>.

If I can't reach any AI provider I move <b>nothing</b> and tell you at \
<b>{alert_at}</b>."""


# -------------------------------------------------------------------- actions

def do_move(cfg, item: dict, folder: str) -> tuple[bool, str]:
    """Apply one reviewed file. Returns (ok, message)."""
    src = Path(item["path"])
    root = Path(item["root"])
    if not src.exists():
        return False, "that file is no longer there"

    allowed = tuple(cfg.scan_roots) + tuple(cfg.media_destinations.values())
    dest, why = safety.resolve_destination(root, folder, src.name, allowed)
    if dest is None:
        return False, f"can't use that folder — {why}"

    try:
        st = src.stat()
    except OSError as e:
        return False, f"unreadable: {e.strerror}"

    record = scanner.FileRecord(
        path=src, root=root, size=st.st_size, mtime=st.st_mtime,
        ctime=getattr(st, "st_birthtime", st.st_ctime),
        ext=src.suffix.lower().lstrip("."))
    move = planner.PlannedMove(
        record=record, dest=dest, category=item.get("category", "manual"),
        folder=folder, confidence=1.0, reason="chosen in Telegram",
        provider="manual")

    run_id = journal.new_run_id()
    jr = journal.Journal(cfg, run_id)
    result = applier.apply_moves(cfg, [move], jr)
    if result.errors:
        return False, result.errors[0][1]
    return True, folder


def trigger_run(cfg, chat_id: int) -> None:
    """Run the organizer in a worker thread so polling stays responsive."""
    def work():
        telegram.send(chat_id, "⏳ Organizing now…")
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "organizer.cli", "run"],
                cwd=str(cfgmod.PROJECT_ROOT), capture_output=True,
                text=True, timeout=cfg.behaviour.run_budget_seconds + 120)
        except subprocess.TimeoutExpired:
            telegram.send(chat_id, "⏱ The run took too long and was stopped. "
                                   "Nothing was left half-done — /status to check.")
            return
        tail = (proc.stdout or proc.stderr or "").strip().splitlines()
        summary = next((l for l in reversed(tail) if "moved" in l.lower()
                        or "failed" in l.lower() or "nothing" in l.lower()), "")
        if proc.returncode == 0:
            telegram.send(chat_id, f"✅ Done. {escape(summary.strip())}"
                          if summary else "✅ Done.",
                          buttons=[[{"text": "📋 Review queue",
                                     "callback_data": "review"}]])
        elif proc.returncode == 5:
            telegram.send(chat_id, "⏳ A run is already in progress.")
        else:
            telegram.send(chat_id, "❌ The run did not finish. "
                                   "Your files were not touched.\n"
                                   f"<code>{escape(summary[:300])}</code>")
    threading.Thread(target=work, daemon=True).start()


# -------------------------------------------------------------------- handlers

def handle_command(cfg, chat_id: int, text: str, session: Session) -> None:
    cmd = text.split()[0].lower().lstrip("/").split("@")[0]

    if cmd in ("start", "help"):
        telegram.send(chat_id, fmt_help(cfg))
        if cmd == "start":
            telegram.send(chat_id, fmt_status(cfg))
        return

    if cmd == "status":
        telegram.send(chat_id, fmt_status(cfg))
        return

    if cmd == "health":
        telegram.send(chat_id, fmt_health(cfg))
        return

    if cmd == "run":
        trigger_run(cfg, chat_id)
        return

    if cmd == "report":
        runs = sorted((cfg.state_dir / "runs").glob("*.md"))
        if not runs:
            telegram.send(chat_id, "No reports yet.")
            return
        body = runs[-1].read_text(encoding="utf-8")
        # Telegram caps a message at 4096 characters.
        head = body[:3500]
        telegram.send(chat_id, f"<b>{escape(runs[-1].stem)}</b>\n\n"
                               f"<pre>{escape(head)}</pre>")
        if len(body) > 3500:
            telegram.send(chat_id, f"<i>…truncated. Full report:</i>\n"
                                   f"<code>{escape(str(runs[-1]))}</code>")
        return

    if cmd == "undo":
        runs = journal.Journal.list_runs(cfg)
        if not runs:
            telegram.send(chat_id, "Nothing to undo.")
            return
        entries = journal.read_journal(runs[-1])
        if not entries:
            telegram.send(chat_id, "The last run moved nothing.")
            return
        session.undo_target = runs[-1].stem
        telegram.send(
            chat_id,
            f"Undo run <b>{escape(runs[-1].stem)}</b>?\n"
            f"That puts <b>{len(entries)}</b> file(s) back where they were.",
            buttons=[[{"text": "↩️ Yes, undo", "callback_data": "undo:yes"},
                      {"text": "Cancel", "callback_data": "undo:no"}]])
        return

    if cmd == "review":
        start_review(cfg, chat_id, session)
        return

    telegram.send(chat_id, "I don't know that one. /help lists what I can do.")


def start_review(cfg, chat_id: int, session: Session) -> None:
    session.queue = load_queue(cfg)
    session.index = 0
    session.awaiting_folder_for = None
    if not session.queue:
        telegram.send(chat_id, "🎉 Nothing waiting — the queue is empty.")
        return
    show_current(cfg, chat_id, session, new_message=True)


def show_current(cfg, chat_id: int, session: Session,
                 new_message: bool = False) -> None:
    item = session.current()
    if item is None:
        done = len(session.queue)
        remaining = load_queue(cfg)
        text = (f"✅ Finished reviewing.\n\n"
                f"<b>{done - len(remaining)}</b> handled, "
                f"<b>{len(remaining)}</b> still waiting.")
        if session.message_id and not new_message:
            telegram.edit(chat_id, session.message_id, text)
        else:
            telegram.send(chat_id, text)
        session.message_id = None
        return

    text, buttons = fmt_review_card(item, session.index, len(session.queue))
    if new_message or session.message_id is None:
        sent = telegram.send(chat_id, text, buttons)
        session.message_id = (sent or {}).get("message_id")
    else:
        telegram.edit(chat_id, session.message_id, text, buttons)


def handle_callback(cfg, chat_id: int, cb: dict, session: Session) -> None:
    data = cb.get("data") or ""
    cb_id = cb["id"]

    if data == "review":
        telegram.answer_callback(cb_id)
        start_review(cfg, chat_id, session)
        return

    if data == "run":
        telegram.answer_callback(cb_id, "Starting…")
        trigger_run(cfg, chat_id)
        return

    if data == "undo:no":
        telegram.answer_callback(cb_id, "Cancelled")
        session.undo_target = None
        return

    if data == "undo:yes":
        telegram.answer_callback(cb_id, "Undoing…")
        # The button on a nightly summary arrives with no session behind it
        # (the bot may have restarted since), so fall back to the newest run.
        if session.undo_target:
            target = cfg.state_dir / "journal" / f"{session.undo_target}.jsonl"
        else:
            runs = journal.Journal.list_runs(cfg)
            target = runs[-1] if runs else None
        session.undo_target = None
        if target is None or not target.exists():
            telegram.send(chat_id, "There is no run left to undo.")
            return
        res = applier.undo_run(cfg, target)
        msg = [f"↩️ Restored <b>{res.restored}</b> file(s)."]
        if res.skipped:
            msg.append(f"{res.skipped} left alone:")
            msg += [f"• {escape(Path(p).name)} — {escape(w)}"
                    for p, w in res.problems[:5]]
        telegram.send(chat_id, "\n".join(msg))
        return

    if not data.startswith("rv:"):
        telegram.answer_callback(cb_id)
        return

    parts = data.split(":")
    action = parts[1]

    if action == "q":
        telegram.answer_callback(cb_id, "Stopped")
        session.index = len(session.queue)
        show_current(cfg, chat_id, session)
        return

    try:
        idx = int(parts[2])
    except (IndexError, ValueError):
        telegram.answer_callback(cb_id)
        return
    if idx != session.index or idx >= len(session.queue):
        telegram.answer_callback(cb_id, "That card is out of date", alert=False)
        return
    item = session.queue[idx]

    if action == "s":
        telegram.answer_callback(cb_id, "Skipped")
        session.index += 1
        show_current(cfg, chat_id, session)
        return

    if action == "d":
        telegram.answer_callback(cb_id)
        text, buttons = fmt_delete_confirm(item, idx)
        if session.message_id:
            telegram.edit(chat_id, session.message_id, text, buttons)
        else:
            sent = telegram.send(chat_id, text, buttons)
            session.message_id = (sent or {}).get("message_id")
        return

    if action == "c":
        telegram.answer_callback(cb_id, "Kept")
        show_current(cfg, chat_id, session)
        return

    if action == "f":
        ok, msg = reveal_in_finder(Path(item["path"]))
        telegram.answer_callback(cb_id, msg if ok else msg[:180], alert=not ok)
        return

    if action == "p":
        telegram.answer_callback(cb_id, "Sending…")
        ok, msg = send_file_for_preview(chat_id, item)
        if not ok:
            excerpt = (item.get("snippet") or "").strip()
            body = [f"❌ {escape(msg)}"]
            if excerpt:
                body += ["", "Here is the text instead:", "",
                         f"<blockquote>{escape(excerpt[:900])}</blockquote>"]
            telegram.send(chat_id, "\n".join(body))
        # Re-post the card so the buttons sit below the file, not above it.
        session.message_id = None
        show_current(cfg, chat_id, session, new_message=True)
        return

    if action == "D":
        src = Path(item["path"])
        try:
            done = trash.send_to_trash(src)
        except trash.TrashError as e:
            telegram.answer_callback(cb_id, str(e)[:180], alert=True)
            show_current(cfg, chat_id, session)
            return
        run_id = journal.new_run_id()
        jr = journal.Journal(cfg, run_id)
        jr.record(journal.MoveEntry(
            src=str(done.original), dst=str(done.trashed_to or ""),
            sha256=done.sha, size=done.size, ts=time.time(), created_dirs=[],
            hash_method=done.method, action="trash",
            category=item.get("category", "manual"), confidence=1.0,
            provider="telegram"))
        telegram.answer_callback(cb_id, "Moved to Trash")
        save_queue(cfg, [i for i in load_queue(cfg) if i["path"] != item["path"]])
        session.index += 1
        show_current(cfg, chat_id, session)
        return

    if action == "e":
        telegram.answer_callback(cb_id)
        session.awaiting_folder_for = idx
        telegram.send(chat_id,
                      f"Which folder should <code>{escape(Path(item['path']).name)}"
                      f"</code> go in?\n\n"
                      "<i>Reply with a name, e.g. <code>Exams/SAT</code>. "
                      "Send <b>-</b> to cancel.</i>")
        return

    if action == "a":
        folder = suggested_folder(item)
        if not folder:
            telegram.answer_callback(cb_id, "No suggestion to accept", alert=True)
            return
        ok, msg = do_move(cfg, item, folder)
        telegram.answer_callback(cb_id, "Moved" if ok else msg[:180],
                                 alert=not ok)
        if ok:
            remaining = [i for i in load_queue(cfg)
                         if i["path"] != item["path"]]
            save_queue(cfg, remaining)
            session.index += 1
        show_current(cfg, chat_id, session)
        return


def handle_folder_reply(cfg, chat_id: int, text: str, session: Session) -> None:
    idx = session.awaiting_folder_for
    session.awaiting_folder_for = None
    if idx is None or idx >= len(session.queue):
        return
    if text.strip() in ("-", "cancel"):
        telegram.send(chat_id, "Cancelled.")
        show_current(cfg, chat_id, session, new_message=True)
        return

    item = session.queue[idx]
    ok, msg = do_move(cfg, item, text.strip())
    if ok:
        telegram.send(chat_id, f"✅ Moved to <b>{escape(msg)}/</b>")
        save_queue(cfg, [i for i in load_queue(cfg) if i["path"] != item["path"]])
        session.index += 1
    else:
        telegram.send(chat_id, f"❌ {escape(msg)}")
    show_current(cfg, chat_id, session, new_message=True)


# ----------------------------------------------------------------------- loop

def run_bot(cfg, once: bool = False) -> int:
    if not telegram.configured():
        print("TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
        return 2

    # Prove we can actually reach the API before claiming to be up. The
    # previous version printed "bot up" and then failed every poll silently.
    try:
        me = telegram.call("getMe", timeout=20, raise_on_error=True)
    except Exception as e:
        print(f"FATAL: cannot reach the Telegram API: {type(e).__name__}: {e}",
              file=sys.stderr, flush=True)
        return 1
    telegram.set_commands()
    owner = telegram.load_chat_id(cfg.state_dir)
    print(f"bot up as @{(me or {}).get('username', '?')}; paired chat: "
          f"{owner if owner else '(none yet — send /start)'}", flush=True)

    sessions: dict[int, Session] = {}
    offset: int | None = None
    consecutive_failures = 0

    # The bot is long-lived, so an edit to its source does nothing until it is
    # restarted — which is exactly how a freshly added button went missing for
    # a quarter of an hour. Notice the change and exit cleanly; launchd's
    # KeepAlive brings it straight back on the new code.
    source_at_start = _source_fingerprint()

    while True:
        if _source_fingerprint() > source_at_start:
            print("source changed on disk — restarting to pick it up",
                  flush=True)
            return 0

        try:
            updates = telegram.get_updates(offset, POLL_TIMEOUT)
            consecutive_failures = 0
        except Exception as e:
            # A blip is normal; a persistent failure is the thing that hid a
            # TLS misconfiguration for days, so escalate rather than loop.
            consecutive_failures += 1
            print(f"poll error #{consecutive_failures}: "
                  f"{type(e).__name__}: {e}", file=sys.stderr, flush=True)
            if consecutive_failures in (5, 50) or consecutive_failures % 200 == 0:
                print(f"STILL FAILING after {consecutive_failures} polls — "
                      "the bot is not receiving anything. Check "
                      "`organize doctor`.", file=sys.stderr, flush=True)
            time.sleep(min(60, 5 * consecutive_failures))
            continue

        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            cb = u.get("callback_query")
            chat = (cb.get("message", {}) if cb else msg).get("chat", {})
            chat_id = chat.get("id")
            if chat_id is None:
                continue

            owner = telegram.load_chat_id(cfg.state_dir)
            text = (msg.get("text") or "").strip()

            # Pairing. With a code on disk (the normal case after `organize
            # setup`) only "/start <code>" can claim the bot, so finding its
            # username is not enough. Without one, the first /start wins.
            if owner is None:
                code = telegram.load_pairing_code(cfg.state_dir)
                if code is not None:
                    claimed = telegram.code_matches(code, text)
                else:
                    claimed = text.lower().startswith("/start")
                if claimed:
                    who = chat.get("username") or chat.get("first_name") or ""
                    telegram.save_chat_id(cfg.state_dir, chat_id, who)
                    telegram.clear_pairing_code(cfg.state_dir)
                    owner = chat_id
                    print(f"paired with chat {chat_id} ({who})", flush=True)
                    telegram.send(chat_id, "🔒 <b>Paired.</b> This bot now answers "
                                           "only to you.")
                    telegram.send(chat_id, fmt_help(cfg))
                    continue
                if code is not None:
                    print(f"refused unpaired chat {chat_id}: no valid code",
                          file=sys.stderr, flush=True)
                    telegram.send(chat_id, "This bot is private. If it is yours, "
                                           "open the pairing link that "
                                           "<code>organize setup</code> printed.")
                else:
                    telegram.send(chat_id, "Send /start to set this bot up.")
                continue
            elif chat_id != owner:
                telegram.send(chat_id, "This bot is private.")
                print(f"refused chat {chat_id}", file=sys.stderr, flush=True)
                continue

            session = sessions.setdefault(chat_id, Session())
            try:
                if cb:
                    handle_callback(cfg, chat_id, cb, session)
                elif session.awaiting_folder_for is not None and text \
                        and not text.startswith("/"):
                    handle_folder_reply(cfg, chat_id, text, session)
                elif text.startswith("/"):
                    handle_command(cfg, chat_id, text, session)
                elif text:
                    telegram.send(chat_id, "Use /help to see what I can do.")
            except Exception as e:
                print(f"handler error: {type(e).__name__}: {e}",
                      file=sys.stderr, flush=True)
                telegram.send(chat_id, "Something went wrong handling that. "
                                       "Your files are untouched.")

        if once:
            return 0
