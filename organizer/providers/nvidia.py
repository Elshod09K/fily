"""NVIDIA NIM provider via the OpenAI-compatible endpoint."""
from __future__ import annotations

import os

from .base import AuthError, NotConfigured, ProviderError, RateLimited, parse_json_array

BASE_URL = "https://integrate.api.nvidia.com/v1"


class NvidiaProvider:
    name = "nvidia"

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("NVIDIA_API_KEY")
        self._client = None

    def available(self) -> bool:
        return bool(self.api_key)

    def _client_or_raise(self):
        if not self.api_key:
            raise NotConfigured("NVIDIA_API_KEY is not set")
        if self._client is None:
            try:
                from openai import OpenAI
            except ImportError as e:
                raise NotConfigured(f"openai sdk not installed: {e}") from e
            self._client = OpenAI(api_key=self.api_key, base_url=BASE_URL)
        return self._client

    def complete_json(self, system: str, user: str, model: str,
                      timeout: int) -> list[dict]:
        client = self._client_or_raise()
        # Not every NIM model honours response_format; fall back to plain text
        # and lean on the tolerant parser rather than failing the attempt.
        for use_json_mode in (True, False):
            kwargs = dict(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.1,
                max_tokens=8192,
                timeout=timeout,
            )
            if use_json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            try:
                resp = client.chat.completions.create(**kwargs)
            except Exception as e:
                if use_json_mode and _is_unsupported_param(e):
                    continue
                raise _translate(e) from e
            content = (resp.choices[0].message.content or "") if resp.choices else ""
            return parse_json_array(content)
        raise ProviderError("no usable request shape for this model")

    def list_models(self) -> list[str]:
        client = self._client_or_raise()
        try:
            return sorted(m.id for m in client.models.list().data)
        except Exception as e:
            raise _translate(e) from e


def _is_unsupported_param(e: Exception) -> bool:
    low = str(e).lower()
    return "response_format" in low or "unsupported" in low or "unrecognized" in low


def _translate(e: Exception) -> ProviderError:
    msg = str(e)
    low = msg.lower()
    code = getattr(e, "status_code", None)
    if code in (401, 403) or "invalid api key" in low or "unauthorized" in low \
            or "authentication" in low:
        return AuthError(msg[:300])
    if code == 429 or "rate limit" in low or "too many requests" in low:
        return RateLimited(msg[:300])
    if code in (404, 410) or "does not exist" in low or "not found" in low \
            or "gone" in low:
        why = "retired" if code == 410 else "unavailable to this account"
        err = ProviderError(f"model {why}: {msg[:230]}")
        err.retryable = False
        return err
    return ProviderError(msg[:300])
