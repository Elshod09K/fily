"""Keep every test hermetic.

Without this, config.load() reads the developer's real .env, so tests quietly
used a real Telegram token and the live network — passing on the machine that
wrote them and failing on a fresh clone. Nothing here may reach the internet,
the real ~/.Trash, launchd, or the real state directory.
"""
from __future__ import annotations

import urllib.request

import pytest

from organizer import config


@pytest.fixture(autouse=True)
def hermetic(tmp_path, monkeypatch):
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "NVIDIA_API_KEY",
                "TELEGRAM_BOT_TOKEN"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "no.env")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path / "state")

    def no_network(*a, **k):
        raise AssertionError("a test tried to reach the network")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    yield
