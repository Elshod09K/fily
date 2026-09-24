"""Provider abstraction and the failover orchestrator.

The chain is configured in config.yaml and tried strictly in order:

    gemini/primary   x5  ->  nvidia/model-a  x5
                         ->  nvidia/model-b  x5   (rotate model, same key)
                         ->  gemini/secondary x3
    all exhausted -> raise AllProvidersFailed; the caller moves nothing.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """Base for provider failures."""

    retryable = True


class AuthError(ProviderError):
    """Bad or missing credentials. Never worth retrying."""

    retryable = False


class NotConfigured(ProviderError):
    """No API key present for this provider."""

    retryable = False


class RateLimited(ProviderError):
    retryable = True


class BadResponse(ProviderError):
    """Empty, truncated or unparseable output."""

    retryable = True


class AllProvidersFailed(RuntimeError):
    def __init__(self, attempts: list["AttemptLog"]):
        self.attempts = attempts
        super().__init__(self._summary())

    def _summary(self) -> str:
        by_step: dict[str, list[str]] = {}
        for a in self.attempts:
            by_step.setdefault(f"{a.provider}/{a.model}", []).append(a.error_type)
        parts = [f"{k}: {len(v)} attempts ({', '.join(sorted(set(v)))})"
                 for k, v in by_step.items()]
        return "every provider in the chain failed -> " + "; ".join(parts)


@dataclass
class AttemptLog:
    provider: str
    model: str
    attempt: int
    ok: bool
    error_type: str = ""
    message: str = ""
    elapsed: float = 0.0


@dataclass
class ChainResult:
    data: list[dict]
    provider: str
    model: str
    attempts: list[AttemptLog] = field(default_factory=list)


class Provider(Protocol):
    name: str

    def available(self) -> bool: ...

    def complete_json(self, system: str, user: str, model: str,
                      timeout: int) -> list[dict]: ...

    def list_models(self) -> list[str]: ...


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json_array(text: str) -> list[dict]:
    """Extract a JSON array of objects from a model response.

    Open-weight models wrap JSON in prose or fences more often than Gemini
    does, so this is deliberately forgiving about the envelope while staying
    strict about the payload.
    """
    if not text or not text.strip():
        raise BadResponse("empty response")
    candidates: list[str] = []
    m = _FENCE.search(text)
    if m:
        candidates.append(m.group(1))
    candidates.append(text)
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])

    for c in candidates:
        c = c.strip()
        if not c:
            continue
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, list):
            return [o for o in obj if isinstance(o, dict)]
        if isinstance(obj, dict):
            for key in ("results", "files", "classifications", "items", "data"):
                v = obj.get(key)
                if isinstance(v, list):
                    return [o for o in v if isinstance(o, dict)]
            return [obj]
    raise BadResponse("no JSON array found in the response")


def _backoff(attempt: int, base: float, cap: float) -> float:
    return min(cap, base * (2 ** (attempt - 1))) * (0.5 + random.random() * 0.5)


def run_chain(cfg, system: str, user: str, log=None) -> ChainResult:
    """Walk the configured chain until one step returns usable JSON."""
    from .gemini import GeminiProvider
    from .nvidia import NvidiaProvider

    providers: dict[str, Provider] = {
        "gemini": GeminiProvider(),
        "nvidia": NvidiaProvider(),
    }
    attempts: list[AttemptLog] = []

    for step in cfg.chain:
        provider = providers.get(step.provider)
        if provider is None:
            attempts.append(AttemptLog(step.provider, step.model, 0, False,
                                       "UnknownProvider", "not implemented"))
            continue
        if not provider.available():
            attempts.append(AttemptLog(step.provider, step.model, 0, False,
                                       "NotConfigured", "no API key in the environment"))
            if log:
                log(f"  {step.provider}/{step.model}: no API key, skipping")
            continue

        for attempt in range(1, step.attempts + 1):
            t0 = time.monotonic()
            try:
                data = provider.complete_json(system, user, step.model,
                                              cfg.timeout_seconds)
                if not data:
                    raise BadResponse("provider returned no records")
                attempts.append(AttemptLog(step.provider, step.model, attempt, True,
                                           elapsed=time.monotonic() - t0))
                if log:
                    log(f"  {step.provider}/{step.model}: ok on attempt {attempt}")
                return ChainResult(data=data, provider=step.provider,
                                   model=step.model, attempts=attempts)
            except Exception as e:
                elapsed = time.monotonic() - t0
                retryable = getattr(e, "retryable", True)
                attempts.append(AttemptLog(step.provider, step.model, attempt, False,
                                           type(e).__name__, str(e)[:300], elapsed))
                if log:
                    log(f"  {step.provider}/{step.model}: attempt {attempt}/"
                        f"{step.attempts} failed ({type(e).__name__}: {str(e)[:120]})")
                if not retryable:
                    if log:
                        log(f"  {step.provider}/{step.model}: not retryable, "
                            "moving to the next provider")
                    break
                if attempt < step.attempts:
                    time.sleep(_backoff(attempt, cfg.backoff_base, cfg.backoff_max))

    raise AllProvidersFailed(attempts)
