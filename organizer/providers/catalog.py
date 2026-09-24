"""Candidate models, and probing which ones a given key can actually use.

A provider's model listing is not evidence: on one NVIDIA key, ten of twelve
listed models answered 404 (not entitled) or 410 (retired). So setup sends a
tiny real request to each candidate and keeps only the ones that answer. The
lists below are starting points; stale entries simply fail the probe.
"""
from __future__ import annotations

import concurrent.futures as cf
import time
from dataclasses import dataclass

GEMINI_CANDIDATES = (
    "gemini-3.8-flash",
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-2.5-flash",
)

NVIDIA_CANDIDATES = (
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "meta/llama-3.3-70b-instruct",
    "openai/gpt-oss-120b",
    "mistralai/mistral-large-2-instruct",
    "qwen/qwen3-235b-a22b",
)

_PROBE_SYSTEM = ('Reply with a JSON array and nothing else: '
                 '[{"file_id":0,"category":"test"}]')
_PROBE_USER = 'Classify: <file id="0">name: example.pdf</file>'


@dataclass
class ProbeResult:
    model: str
    ok: bool
    seconds: float
    error: str = ""


def _probe_one(provider, model: str, timeout: int) -> ProbeResult:
    t0 = time.monotonic()
    try:
        provider.complete_json(_PROBE_SYSTEM, _PROBE_USER, model, timeout)
        return ProbeResult(model, True, time.monotonic() - t0)
    except Exception as e:
        return ProbeResult(model, False, time.monotonic() - t0,
                           f"{type(e).__name__}: {str(e)[:120]}")


def probe(provider, candidates, timeout: int = 45) -> list[ProbeResult]:
    """Probe every candidate concurrently; results keep candidate order."""
    with cf.ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
        futures = [ex.submit(_probe_one, provider, m, timeout) for m in candidates]
        return [f.result() for f in futures]


def check_key(provider) -> tuple[bool, str]:
    """Is the key itself valid? Cheaper and clearer than a failed probe."""
    try:
        provider.list_models()
        return True, ""
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        if "api key" in low or "unauthor" in low or "401" in low or "403" in low \
                or "invalid" in low:
            return False, "the key was rejected"
        return False, f"{type(e).__name__}: {msg[:140]}"


def build_chain(gemini_ok: list[str], nvidia_ok: list[str]) -> list[dict]:
    """Primary Gemini, then up to two NVIDIA models, then a second Gemini.

    Five attempts on primaries and three on the last resort, matching the
    failover behaviour the project was designed around.
    """
    chain: list[dict] = []
    if gemini_ok:
        chain.append({"provider": "gemini", "model": gemini_ok[0], "attempts": 5})
    for m in nvidia_ok[:2]:
        chain.append({"provider": "nvidia", "model": m, "attempts": 5})
    if len(gemini_ok) > 1:
        chain.append({"provider": "gemini", "model": gemini_ok[1], "attempts": 3})
    return chain
