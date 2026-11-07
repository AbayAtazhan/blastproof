"""Minimal async client for Vultr Serverless Inference (OpenAI-compatible API)."""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Protocol

import httpx

from .config import Settings

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


class LLMError(RuntimeError):
    pass


@dataclass
class Completion:
    text: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int


class ChatModel(Protocol):
    model: str

    async def complete(self, messages: list[dict], *, temperature: float = 0.1,
                       max_tokens: int = 2048, model: str | None = None) -> Completion: ...


class VultrInference:
    """Talks to https://api.vultrinference.com/v1 with retries and sane timeouts."""

    def __init__(self, settings: Settings):
        self.base_url = settings.inference_base_url
        self.model = settings.agent_model
        self._key = settings.inference_api_key
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(180.0, connect=15.0),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )

    @property
    def configured(self) -> bool:
        return bool(self._key)

    async def aclose(self):
        await self._client.aclose()

    async def complete(self, messages: list[dict], *, temperature: float = 0.1,
                       max_tokens: int = 2048, model: str | None = None) -> Completion:
        if not self._key:
            raise LLMError("VULTR_INFERENCE_API_KEY is not set")
        body = {"model": model or self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens}
        delay = 1.5
        last_err = "unknown error"
        for attempt in range(4):
            started = time.monotonic()
            try:
                resp = await self._client.post(f"{self.base_url}/chat/completions", json=body)
            except httpx.HTTPError as exc:
                last_err = f"network error talking to inference: {exc.__class__.__name__}"
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    choice = (data.get("choices") or [{}])[0]
                    msg = choice.get("message") or {}
                    text = msg.get("content") or ""
                    if isinstance(text, list):  # some servers return content parts
                        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
                    text = _THINK.sub("", text).strip()
                    if not text:
                        # Reasoning models sometimes spend the whole budget thinking and
                        # return no content. Give them more room and ask again.
                        last_err = "model returned an empty answer (token budget spent on reasoning)"
                        body["max_tokens"] = min(int(body["max_tokens"] * 1.8), 16000)
                        continue
                    usage = data.get("usage") or {}
                    return Completion(text=text, model=data.get("model", body["model"]),
                                      prompt_tokens=usage.get("prompt_tokens", 0),
                                      completion_tokens=usage.get("completion_tokens", 0),
                                      latency_ms=int((time.monotonic() - started) * 1000))
                last_err = f"inference HTTP {resp.status_code}: {resp.text[:300]}"
                if resp.status_code not in (408, 409, 429, 500, 502, 503, 504):
                    break
            await asyncio.sleep(delay)
            delay *= 2
        raise LLMError(last_err)

    async def list_models(self) -> list[str]:
        resp = await self._client.get(f"{self.base_url}/models")
        resp.raise_for_status()
        return [m.get("id") for m in resp.json().get("data", [])]
