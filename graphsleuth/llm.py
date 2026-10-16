"""LLM transport.

`LiveBrain` talks to any OpenAI-compatible /chat/completions endpoint.
`ScriptedBrain` is a deterministic stand-in used for the offline demo mode —
it performs the same tool calls a real model would, so the whole system is
runnable (and demoable) without an API key.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .config import config


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


class LiveBrain:
    """Calls a real model over an OpenAI-compatible HTTP API."""

    def __init__(self) -> None:
        if not config.has_llm_key:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env or run with LLM_MODE=demo."
            )

    def chat(self, messages: list[dict], tools: list[dict], history: list[dict] | None = None) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": config.openai_model,
            "messages": messages,
            "temperature": 0.1,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = requests.post(
            f"{config.openai_base_url}/chat/completions",
            json=payload,
            headers={
                "Authorization": f"Bearer {config.openai_api_key}",
                "Content-Type": "application/json",
            },
            timeout=180,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"LLM request failed ({response.status_code}): {response.text[:500]}")

        message = response.json()["choices"][0]["message"]
        calls: list[ToolCall] = []
        for index, call in enumerate(message.get("tool_calls") or []):
            function = call.get("function", {}) or {}
            raw_args = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                arguments = {"_raw_arguments": raw_args}
            calls.append(
                ToolCall(
                    id=call.get("id") or f"call_{index}",
                    name=function.get("name", ""),
                    arguments=arguments,
                )
            )
        return LLMResponse(content=message.get("content"), tool_calls=calls)


class ScriptedBrain:
    """Deterministic brain for demo/offline mode.

    `plan` is a list of `(tool_name, args)` where `args` may be a dict or a
    callable `history -> dict` so later calls can use earlier results.
    When the plan runs out, `on_finish(question, history)` builds the answer.
    """

    def __init__(
        self,
        plan: list[tuple[str, Any]],
        on_finish: Callable[[str, list[dict]], str],
        preamble: str | None = None,
    ) -> None:
        self._queue = list(plan)
        self._on_finish = on_finish
        self._preamble = preamble
        self._step = 0

    def chat(self, messages: list[dict], tools: list[dict], history: list[dict] | None = None) -> LLMResponse:
        history = history or []
        question = _last_user_message(messages)

        if self._step == 0 and self._preamble:
            self._step = 1
            return LLMResponse(content=self._preamble)

        if self._queue:
            name, args = self._queue.pop(0)
            resolved = args(history) if callable(args) else dict(args)
            self._step += 1
            return LLMResponse(
                tool_calls=[ToolCall(id=f"demo_{self._step}", name=name, arguments=resolved)],
                content=None,
            )

        return LLMResponse(content=self._on_finish(question, history))


def _last_user_message(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


def get_brain(scripted: ScriptedBrain | None = None):
    """Pick the brain: live model when a key is configured, else the scripted one."""
    if config.use_live_llm():
        return LiveBrain()
    if scripted is None:
        raise RuntimeError(
            "Demo mode needs a scripted brain, but none was provided. "
            "Set OPENAI_API_KEY in .env to use a real model."
        )
    return scripted
