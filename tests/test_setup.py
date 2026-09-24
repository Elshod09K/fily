"""Tests for installing, configuring and pairing — the parts a new user hits
first, where a silent failure costs the most trust."""
from __future__ import annotations

import os
import plistlib
import stat
from pathlib import Path

import pytest
import yaml

from organizer import config, launchd, notify, scanner, setup, telegram
from organizer.providers import catalog


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "Downloads"
    root.mkdir()
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({
        "scan_roots": [str(root)], "media_destinations": {}, "deny_paths": [],
        "behaviour": {"quarantine_hours": 24},
        "schedule": {"run": "21:30", "alert": "07:45"},
        "providers": {"chain": [{"provider": "gemini", "model": "x"}]},
    }))
    return config.load(p)


# -------------------------------------------------------------------- config

@pytest.mark.parametrize("bad", ["25:00", "12:60", "noon", "", "12"])
def test_schedule_rejects_nonsense(bad):
    with pytest.raises(config.ConfigError):
        config.parse_hhmm(bad, "schedule.run")


def test_unquoted_yaml_time_is_accepted():
    """YAML 1.1 turns an unquoted 22:00 into 1320; hand-edited configs do this."""
    value = yaml.safe_load("run: 22:00")["run"]
    assert value == 1320
    assert config.parse_hhmm(value, "schedule.run") == (22, 0)


@pytest.mark.parametrize("err,phrase", [
    ("ProviderError: model retired: Error code: 410 - {...}", "retired"),
    ("ProviderError: model unavailable to this account: 404", "not available"),
    ("ProviderError: Request timed out.", "too slow"),
])
def test_probe_errors_are_translated(err, phrase):
    assert phrase in setup._why_not(err)


def test_schedule_is_read(cfg):
    assert cfg.schedule.run_at == "21:30"
    assert cfg.schedule.alert_at == "07:45"


def test_deep_merge_overrides_and_keeps_defaults():
    base = {"a": 1, "nest": {"x": 1, "y": 2}, "lst": [1, 2]}
    out = config.deep_merge(base, {"nest": {"y": 9}, "lst": [3]})
    assert out == {"a": 1, "nest": {"x": 1, "y": 9}, "lst": [3]}


def test_missing_personal_config_says_run_setup(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEFAULT_CONFIG", tmp_path / "config.yaml")
    with pytest.raises(config.ConfigError, match="organize setup"):
        config.load()


def test_example_config_is_valid_and_generic():
    raw = yaml.safe_load(config.EXAMPLE_CONFIG.read_text())
    assert raw["scan_roots"] == ["~/Downloads", "~/Desktop"]
    text = config.EXAMPLE_CONFIG.read_text()
    assert "/Users/" not in text, "the template contains a machine path"


# ------------------------------------------------------------------- launchd

def test_plists_carry_no_secrets_and_the_right_times(cfg, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaSECRET")
    for job in launchd.JOBS:
        blob = plistlib.dumps(launchd.build(job, cfg)).decode()
        assert "SECRET" not in blob, f"{job} plist leaks a secret"
    run = launchd.build("run", cfg)["StartCalendarInterval"]
    assert (run["Hour"], run["Minute"]) == (21, 30)
    alert = launchd.build("alert", cfg)["StartCalendarInterval"]
    assert (alert["Hour"], alert["Minute"]) == (7, 45)
    bot = launchd.build("bot", cfg)
    assert bot["KeepAlive"] is True and "StartCalendarInterval" not in bot


def test_plists_point_at_this_copy(cfg):
    d = launchd.build("run", cfg)
    assert d["WorkingDirectory"] == str(config.PROJECT_ROOT)
    assert d["ProgramArguments"][-1] == "run"
    assert Path(d["StandardOutPath"]).parent == cfg.state_dir / "logs"


def test_labels_are_generic():
    for job in launchd.JOBS:
        assert launchd.label(job).startswith("local.fily.")


# --------------------------------------------------------------- permissions

def test_unreadable_root_is_reported_not_treated_as_empty(cfg):
    """os.walk swallows permission errors and yields nothing, so a folder
    macOS hides from a background job looked exactly like an empty one."""
    root = cfg.scan_roots[0]
    (root / "a.pdf").write_text("x")
    os.chmod(root, 0)
    try:
        res = scanner.scan(cfg)
    finally:
        os.chmod(root, 0o755)
    assert res.files == []
    assert res.unreadable_roots and res.unreadable_roots[0][0] == root


def test_readable_root_is_not_flagged(cfg):
    res = scanner.scan(cfg)
    assert res.unreadable_roots == []


# ------------------------------------------------------------------- pairing

def test_pairing_code_is_private_and_deep_link_safe(tmp_path):
    code = telegram.new_pairing_code(tmp_path)
    assert all(c.isalnum() or c in "-_" for c in code) and len(code) >= 12
    mode = stat.S_IMODE((tmp_path / "pairing_code").stat().st_mode)
    assert mode == 0o600


@pytest.mark.parametrize("text,ok", [
    ("/start CODE123", True),
    ("/start", False),                 # found the bot, but has no link
    ("/start WRONG", False),
    ("CODE123", False),
    ("/start CODE123 extra", False),
])
def test_code_matching(text, ok):
    assert telegram.code_matches("CODE123", text) is ok


def _drive_bot(cfg, monkeypatch, updates):
    from organizer import bot
    sent = []
    monkeypatch.setattr(telegram, "configured", lambda: True)
    monkeypatch.setattr(telegram, "set_commands", lambda: None)
    monkeypatch.setattr(telegram, "call",
                        lambda m, *a, **k: {"username": "b"} if m == "getMe" else {})
    monkeypatch.setattr(telegram, "send",
                        lambda cid, text, *a, **k: sent.append((cid, text)))
    monkeypatch.setattr(telegram, "get_updates", lambda offset=None, timeout=0: updates)
    bot.run_bot(cfg, once=True)
    return sent


def test_start_without_the_code_cannot_claim_the_bot(cfg, monkeypatch):
    telegram.new_pairing_code(cfg.state_dir)
    sent = _drive_bot(cfg, monkeypatch, [
        {"update_id": 1, "message": {"chat": {"id": 666}, "text": "/start"}}])
    assert telegram.load_chat_id(cfg.state_dir) is None
    assert "private" in sent[-1][1].lower()


def test_start_with_the_code_pairs_and_burns_it(cfg, monkeypatch):
    code = telegram.new_pairing_code(cfg.state_dir)
    _drive_bot(cfg, monkeypatch, [
        {"update_id": 1, "message": {"chat": {"id": 42, "username": "me"},
                                     "text": f"/start {code}"}}])
    assert telegram.load_chat_id(cfg.state_dir) == 42
    assert telegram.load_pairing_code(cfg.state_dir) is None, "code reusable"


# --------------------------------------------------------------- setup files

def test_env_is_owner_only_and_merges(tmp_path):
    env = tmp_path / ".env"
    env.write_text("KEEP_ME=1\nGEMINI_API_KEY=old\n")
    setup.write_env({"GEMINI_API_KEY": "new", "TELEGRAM_BOT_TOKEN": "t"}, env)
    vals = setup.read_env(env)
    assert vals == {"KEEP_ME": "1", "GEMINI_API_KEY": "new", "TELEGRAM_BOT_TOKEN": "t"}
    assert stat.S_IMODE(env.stat().st_mode) == 0o600


def test_empty_value_removes_a_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NVIDIA_API_KEY=x\n")
    setup.write_env({"NVIDIA_API_KEY": ""}, env)
    assert "NVIDIA_API_KEY" not in setup.read_env(env)


def test_write_config_keeps_unrelated_settings(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    monkeypatch.setattr(config, "DEFAULT_CONFIG", path)
    path.write_text(yaml.safe_dump({"deny_paths": ["~/Secret"],
                                    "duplicates": {"action": "stage"}}))
    setup.write_config(["~/Downloads"], "20:15",
                       [{"provider": "gemini", "model": "m", "attempts": 5}])
    data = yaml.safe_load(path.read_text())
    assert data["deny_paths"] == ["~/Secret"]
    assert data["duplicates"] == {"action": "stage"}
    assert data["schedule"]["run"] == "20:15"
    assert (tmp_path / "config.yaml.bak").exists()


def test_minus_wraps_midnight():
    assert setup._minus("00:01", 3) == "23:58"
    assert setup._minus("22:00", 3) == "21:57"


# ------------------------------------------------------------------ catalog

def test_chain_uses_gemini_first_then_nvidia_then_second_gemini():
    chain = catalog.build_chain(["g1", "g2"], ["n1", "n2", "n3"])
    assert [(s["provider"], s["model"]) for s in chain] == [
        ("gemini", "g1"), ("nvidia", "n1"), ("nvidia", "n2"), ("gemini", "g2")]


def test_chain_with_only_nvidia():
    assert [s["model"] for s in catalog.build_chain([], ["n1"])] == ["n1"]


def test_chain_empty_when_nothing_works():
    assert catalog.build_chain([], []) == []


# --------------------------------------------------------------- deliveries

def test_every_push_is_logged(cfg, monkeypatch):
    monkeypatch.setattr(telegram, "configured", lambda: True)
    telegram.save_chat_id(cfg.state_dir, 1)
    monkeypatch.setattr(telegram, "send", lambda *a, **k: None)   # fails
    assert notify.telegram_push(cfg, "<b>nightly</b> report") is False
    last = notify.last_delivery(cfg)
    assert last["ok"] is False and last["text"] == "nightly report"
