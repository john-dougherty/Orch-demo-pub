"""LLM abstraction: Ollama primary, cloud fallback.

Fallback triggers (in order):
  1. Ollama unreachable or returns error.
  2. Caller explicitly requests higher tier (e.g. a tool-use flow the local
     model has been observed to fumble).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import httpx
import ollama

from hermes.config import settings

log = logging.getLogger(__name__)


@dataclass
class LLMResponse:
    text: str
    source: str  # "ollama" | "fallback"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class LLMClient:
    def __init__(self) -> None:
        self._ollama = ollama.Client(
            host=settings.ollama_base_url,
            timeout=settings.ollama_timeout_seconds,
        )

    # --- basic chat ---

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        force_fallback: bool = False,
        temperature: float = 0.3,
        format_json: bool = False,
    ) -> LLMResponse:
        if not force_fallback:
            try:
                kwargs: dict[str, Any] = {
                    "model": settings.ollama_model,
                    "messages": messages,
                    "options": {"temperature": temperature},
                }
                if format_json:
                    kwargs["format"] = "json"
                resp = self._ollama.chat(**kwargs)
                return LLMResponse(
                    text=resp["message"].get("content", "") or "",
                    source="ollama",
                    raw=dict(resp),
                )
            except (ollama.ResponseError, httpx.HTTPError, ConnectionError) as e:
                log.warning("Ollama chat failed, falling back: %s", e)

        return self._call_fallback(messages, temperature=temperature)

    # --- native tool-use chat (Ollama's built-in tool_calls path) ---

    def chat_with_tools_native(
        self,
        messages: list[dict[str, Any]],
        tools_spec: list[dict[str, Any]],
        *,
        temperature: float = 0.3,
    ) -> LLMResponse:
        try:
            resp = self._ollama.chat(
                model=settings.ollama_model,
                messages=messages,
                tools=tools_spec,
                options={"temperature": temperature},
            )
        except (ollama.ResponseError, httpx.HTTPError, ConnectionError) as e:
            log.warning("Ollama tool-call chat failed, falling back: %s", e)
            return self._call_fallback(messages, temperature=temperature)

        msg = resp.get("message", {}) or {}
        raw_calls = msg.get("tool_calls") or []
        # Normalize: Ollama returns [{"function": {"name": ..., "arguments": {...}}}]
        normalized: list[dict[str, Any]] = []
        for tc in raw_calls:
            fn = tc.get("function", {})
            normalized.append(
                {"name": fn.get("name"), "arguments": fn.get("arguments") or {}}
            )
        return LLMResponse(
            text=msg.get("content", "") or "",
            source="ollama",
            tool_calls=normalized,
            raw=dict(resp),
        )

    # --- fallback ---

    def _call_fallback(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float,
    ) -> LLMResponse:
        if not settings.fallback_api_key:
            raise RuntimeError("Fallback LLM requested but FALLBACK_API_KEY is not set")

        if settings.fallback_provider == "xai":
            from openai import OpenAI

            client = OpenAI(
                api_key=settings.fallback_api_key,
                base_url="https://api.x.ai/v1",
            )
            # Strip any ollama-specific fields the OpenAI schema won't accept.
            clean_msgs = [
                {k: v for k, v in m.items() if k in ("role", "content", "name")}
                for m in messages
            ]
            resp = client.chat.completions.create(
                model=settings.fallback_model,
                messages=clean_msgs,  # type: ignore[arg-type]
                temperature=temperature,
            )
            return LLMResponse(
                text=resp.choices[0].message.content or "",
                source="fallback",
                raw=resp.model_dump(),
            )

        raise NotImplementedError(f"Fallback provider {settings.fallback_provider!r} not wired")


llm = LLMClient()
