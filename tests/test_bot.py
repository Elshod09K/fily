"""Tests for the Telegram front end.

The important ones are about who the bot will talk to. It can list and move a
real home directory, and a bot username is discoverable, so the pairing lock is
the only thing standing between a stranger and someone's files.
"""
from __future__ import annotations


from pathlib import Path

import pytest
import yaml

from organizer import bot, config, telegram


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    root = tmp_path / "Downloads"
    root.mkdir()
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump({
        "scan_roots": [str(root)],
        "media_destinations": {}, "deny_paths": [],
        "behaviour": {"quarantine_hours": 24},
        "providers": {"chain": [{"provider": "gemini", "model": "x"}]},
    }))
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")
    c = config.load(cfg_path)
    (tmp_path / "state").mkdir(exist_ok=True)
    return c


# ------------------------------------------------------------------- pairing

def test_unpaired_then_first_start_claims_it(cfg):
    assert telegram.load_chat_id(cfg.state_dir) is None
    telegram.save_chat_id(cfg.state_dir, 111, "owner")
    assert telegram.load_chat_id(cfg.state_dir) == 111


def test_pairing_file_is_owner_only(cfg):
    from organizer import host
    telegram.save_chat_id(cfg.state_dir, 111, "owner")
    assert host.owner_only(telegram.pairing_path(cfg.state_dir))


def test_unpair_allows_reclaiming(cfg):
    telegram.save_chat_id(cfg.state_dir, 111)
    telegram.clear_chat_id(cfg.state_dir)
    assert telegram.load_chat_id(cfg.state_dir) is None
    telegram.save_chat_id(cfg.state_dir, 222)
    assert telegram.load_chat_id(cfg.state_dir) == 222


def test_corrupt_pairing_file_does_not_authorise_anyone(cfg):
    telegram.pairing_path(cfg.state_dir).write_text("{ not json")
    assert telegram.load_chat_id(cfg.state_dir) is None


def _fake_api(monkeypatch):
    monkeypatch.setattr(telegram, "call",
                        lambda method, *a, **k: {"username": "test_bot"}
                        if method == "getMe" else {})


def test_a_stranger_is_refused(cfg, monkeypatch):
    """The whole point: a second chat must get nothing."""
    telegram.save_chat_id(cfg.state_dir, 111, "owner")
    _fake_api(monkeypatch)

    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(telegram, "send",
                        lambda cid, text, *a, **k: sent.append((cid, text)))
    monkeypatch.setattr(telegram, "configured", lambda: True)
    monkeypatch.setattr(telegram, "set_commands", lambda: None)

    stranger = {"update_id": 1, "message": {
        "chat": {"id": 999, "username": "someone_else"}, "text": "/status"}}
    updates = [stranger]
    monkeypatch.setattr(telegram, "get_updates",
                        lambda offset=None, timeout=0: updates)

    bot.run_bot(cfg, once=True)

    assert sent, "the stranger got no reply at all"
    assert all(cid == 999 for cid, _ in sent)
    assert "private" in sent[-1][1].lower()
    # Nothing about the machine's contents leaked.
    assert not any("waiting" in t or "Downloads" in t for _, t in sent)


def test_owner_gets_a_real_answer(cfg, monkeypatch):
    telegram.save_chat_id(cfg.state_dir, 111, "owner")
    _fake_api(monkeypatch)
    sent: list[tuple[int, str]] = []
    monkeypatch.setattr(telegram, "send",
                        lambda cid, text, *a, **k: sent.append((cid, text)))
    monkeypatch.setattr(telegram, "configured", lambda: True)
    monkeypatch.setattr(telegram, "set_commands", lambda: None)
    monkeypatch.setattr(telegram, "get_updates", lambda offset=None, timeout=0: [
        {"update_id": 1, "message": {"chat": {"id": 111}, "text": "/status"}}])

    bot.run_bot(cfg, once=True)
    assert sent and sent[0][0] == 111
    assert "File Organizer" in sent[0][1]


# ------------------------------------------------------------ folder handling

@pytest.mark.parametrize("suggestion,expected", [
    ("Exams/SAT", "Exams/SAT"),
    ("", ""),
])
def test_suggested_folder_relative(suggestion, expected):
    item = {"path": "/x/Downloads/a.pdf", "root": "/x/Downloads",
            "suggestion": suggestion}
    assert bot.suggested_folder(item) == expected


def test_suggested_folder_from_absolute_path(tmp_path):
    root = tmp_path / "Downloads"                    # absolute on either OS
    item = {"path": str(root / "a.pdf"), "root": str(root),
            "suggestion": str(root / "Exams" / "SAT" / "a.pdf")}
    assert Path(bot.suggested_folder(item)) == Path("Exams/SAT")


@pytest.mark.parametrize("typed", ["../../etc", "/etc", "~/Library", ".."])
def test_a_typed_folder_cannot_escape(cfg, typed):
    """A folder typed into Telegram goes through the same gate as the model's."""
    root = cfg.scan_roots[0]
    f = root / "doc.pdf"
    f.write_bytes(b"x")
    item = {"path": str(f), "root": str(root), "category": "unknown"}
    ok, msg = bot.do_move(cfg, item, typed)
    assert not ok, f"{typed!r} was accepted"
    assert f.exists(), "the file was moved despite the rejection"


def test_a_sensible_typed_folder_works(cfg):
    root = cfg.scan_roots[0]
    f = root / "doc.pdf"
    f.write_bytes(b"x")
    item = {"path": str(f), "root": str(root), "category": "unknown"}
    ok, msg = bot.do_move(cfg, item, "Papers/Drafts")
    assert ok, msg
    assert (root / "Papers" / "Drafts" / "doc.pdf").exists()
    assert not f.exists()


def test_escape_neutralises_html(cfg):
    assert telegram.escape("<b>x</b> & y") == "&lt;b&gt;x&lt;/b&gt; &amp; y"


# ------------------------------------------------------- failure visibility

def test_get_updates_raises_instead_of_looking_empty(monkeypatch):
    """Regression: get_updates used to swallow every error and return [].

    A TLS failure was therefore indistinguishable from "no new messages", so
    the bot span for days while updates piled up on Telegram's side. A broken
    poll must be loud.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x:y")

    def boom(*a, **k):
        raise OSError("SSL: CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", boom)

    with pytest.raises(telegram.TelegramError):
        telegram.get_updates(None, 0)


def test_send_still_fails_soft(monkeypatch):
    """Sending is different: a failed notification must never break a run."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x:y")

    def boom(*a, **k):
        raise OSError("network down")

    monkeypatch.setattr(telegram.urllib.request, "urlopen", boom)
    assert telegram.send(123, "hello") is None      # no exception


def test_ssl_context_uses_certifi():
    """urllib needs an explicit bundle; the system default may verify nothing."""
    import ssl
    ctx = telegram._ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.get_ca_certs(), "context has no CA certificates loaded"


# --------------------------------------------------------- inspecting a file

def test_reveal_asks_the_os_file_manager(tmp_path, monkeypatch):
    """`file://` links are not clickable in Telegram, so the bot asks Finder
    or File Explorer directly — it runs on the same machine as the files."""
    from organizer import host
    calls = []
    monkeypatch.setattr(host, "reveal", lambda p: calls.append(p))
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"x")

    ok, msg = bot.reveal_in_file_manager(f)
    assert ok, msg
    assert calls == [f]
    assert host.FILE_MANAGER in msg


def test_reveal_reports_a_missing_file(tmp_path, monkeypatch):
    from organizer import host
    monkeypatch.setattr(host, "reveal", lambda p: None)
    ok, msg = bot.reveal_in_file_manager(tmp_path / "gone.pdf")
    assert not ok and "no longer" in msg

def test_reveal_commands_are_right_per_os(monkeypatch, tmp_path):
    """The two real implementations, checked without running either."""
    from organizer.host import macos, windows
    seen = []
    monkeypatch.setattr(macos.subprocess, "run", lambda cmd, **k: seen.append(cmd))
    macos.reveal(tmp_path / "a b.pdf")
    assert seen[-1][:2] == ["/usr/bin/open", "-R"]
    monkeypatch.setattr(windows.subprocess, "run", lambda cmd, **k: seen.append(cmd))
    windows.reveal(tmp_path / "a b.pdf")
    assert seen[-1].startswith('explorer.exe /select,"') and seen[-1].endswith('a b.pdf"')


def test_preview_refuses_oversized_files(tmp_path, monkeypatch):
    """Telegram caps bot uploads; a 370 MB installer must fail cleanly rather
    than hang a poll cycle uploading it."""
    uploaded = []
    monkeypatch.setattr(telegram, "send_document",
                        lambda *a, **k: uploaded.append(a) or {})
    monkeypatch.setattr(bot, "MAX_UPLOAD_BYTES", 1024)

    big = tmp_path / "huge.dmg"
    big.write_bytes(b"x" * 4096)
    ok, msg = bot.send_file_for_preview(1, {"path": str(big),
                                            "root": str(tmp_path)})
    assert not ok and "too big" in msg
    assert uploaded == [], "it tried to upload anyway"


def test_preview_uploads_a_normal_file(tmp_path, monkeypatch):
    uploaded = []
    monkeypatch.setattr(telegram, "send_document",
                        lambda cid, p, caption="": uploaded.append(p) or {"ok": 1})
    f = tmp_path / "small.pdf"
    f.write_bytes(b"hello")
    ok, msg = bot.send_file_for_preview(1, {"path": str(f),
                                            "root": str(tmp_path)})
    assert ok, msg
    assert uploaded == [f]


def test_card_shows_an_excerpt_when_there_is_one():
    item = {"path": "/x/Downloads/mystery.pdf", "root": "/x/Downloads",
            "why": "confidence 0.70", "suggestion": "Papers",
            "size": 1024, "snippet": "Molding-sand mixture properties for "
                                     "20GL steel castings, chapter two."}
    text, buttons = bot.fmt_review_card(item, 0, 1)
    assert "Molding-sand" in text
    assert "blockquote" in text
    labels = [b["text"] for row in buttons for b in row]
    from organizer import host
    assert any(host.FILE_MANAGER in l for l in labels)
    assert any("Send me it" in l for l in labels)


def test_card_falls_back_to_the_note_when_there_is_no_text():
    item = {"path": "/x/Downloads/scan.pdf", "root": "/x/Downloads",
            "why": "confidence 0.70", "suggestion": "Papers", "size": 10,
            "snippet": "", "snippet_note": "no extractable text (likely a scan)"}
    text, _ = bot.fmt_review_card(item, 0, 1)
    assert "likely a scan" in text
