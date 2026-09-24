"""Gemini provider (google-genai), primary link in the chain."""
from __future__ import annotations

import logging
import os

from .base import AuthError, BadResponse, NotConfigured, ProviderError, RateLimited, parse_json_array

# The SDK warns about automatic function calling on every generate_content call.
# We pass no tools, so it does not apply; silence it to keep run output readable.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or \
            os.environ.get("GOOGLE_API_KEY")
        self._client = None
        self._thinking: dict[str, list[str | None]] = {}

    def available(self) -> bool:
        return bool(self.api_key)

    def _client_or_raise(self):
        if not self.api_key:
            raise NotConfigured("GEMINI_API_KEY is not set")
        if self._client is None:
            try:
                from google import genai
            except ImportError as e:
                raise NotConfigured(f"google-genai not installed: {e}") from e
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    def _config(self, system: str, timeout: int, thinking: str | None):
        """Build a request config, optionally suppressing model thinking.

        Sorting files is recall, not reasoning. Newer Gemini models think by
        default, which adds latency and tokens for no gain here, so we ask for
        the cheapest setting the model accepts. Different generations spell
        that differently, hence the variants.
        """
        from google.genai import types

        kwargs = dict(
            system_instruction=system,
            temperature=0.1,
            response_mime_type="application/json",
            http_options=types.HttpOptions(timeout=timeout * 1000),
        )
        if thinking == "level":
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="low")
        elif thinking == "budget":
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)
        return types.GenerateContentConfig(**kwargs)

    def complete_json(self, system: str, user: str, model: str,
                      timeout: int) -> list[dict]:
        client = self._client_or_raise()

        # Try the cheapest thinking setting first, then fall back. The working
        # variant is remembered per model so this costs one probe, once.
        variants = self._thinking.get(model) or ["level", "budget", None]
        resp = None
        last: Exception | None = None
        for variant in variants:
            try:
                resp = client.models.generate_content(
                    model=model, contents=user,
                    config=self._config(system, timeout, variant),
                )
                self._thinking[model] = [variant]
                break
            except Exception as e:
                last = e
                if _is_unsupported_config(e):
                    continue
                raise _translate(e) from e
        if resp is None:
            raise _translate(last) from last

        text = getattr(resp, "text", None)
        if not text:
            # A blocked or empty candidate is worth one more shot.
            feedback = getattr(resp, "prompt_feedback", None)
            raise BadResponse(f"no text in response (feedback={feedback})")
        return parse_json_array(text)

    def list_models(self) -> list[str]:
        client = self._client_or_raise()
        out: list[str] = []
        try:
            for m in client.models.list():
                actions = getattr(m, "supported_actions", None) or []
                if not actions or "generateContent" in actions:
                    out.append((m.name or "").removeprefix("models/"))
        except Exception as e:
            raise _translate(e) from e
        return sorted(n for n in out if n)


def _is_unsupported_config(e: Exception) -> bool:
    """True when the model rejected a config field rather than the request."""
    low = str(e).lower()
    return (("thinking" in low or "unknown field" in low or "unsupported" in low
             or "not supported" in low or "invalid_argument" in low)
            and "deadline" not in low)


def _translate(e: Exception) -> ProviderError:
    msg = str(e)
    low = msg.lower()
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    if code in (401, 403) or "api key not valid" in low or "permission denied" in low \
            or "unauthenticated" in low:
        return AuthError(msg[:300])
    if code == 429 or "resource_exhausted" in low or "rate limit" in low or "quota" in low:
        return RateLimited(msg[:300])
    if code == 404 or "not found" in low:
        # A wrong model id is a config problem; move on rather than retry it 5x.
        err = ProviderError(f"model unavailable: {msg[:250]}")
        err.retryable = False
        return err
    return ProviderError(msg[:300])
