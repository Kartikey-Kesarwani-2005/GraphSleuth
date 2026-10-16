"""LLM transport.

`LiveBrain` talks to any OpenAI-compatible /chat/completions endpoint.
`ScriptedBrain` is a deterministic stand-in used for the offline fallback —
it performs the same tool calls a real model would, so the whole system is
runnable without an API key.
"""

from __future__ import annotations

import json
import re
import time
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
    """Calls a real model over an OpenAI-compatible HTTP API.

    Works against Groq, OpenAI, OpenRouter, Gemini or a local Ollama — anything
    that speaks `/chat/completions`. The endpoint comes from `config.llm_*`
    (GROQ_API_KEY wins, then OPENAI_API_KEY).

    Free tiers rate-limit aggressively (Groq: 8k tokens/minute *and* a
    per-model tokens/day cap), and an agent loop burns tokens fast, so the
    transport retries 429/5xx with the delay the API asks for — and when a
    model hits its **daily** cap it walks the fallback chain
    (`config.llm_models`) to the next model rather than stalling the run.
    """

    MAX_RETRIES = 6

    def __init__(self, model: str | None = None) -> None:
        if not config.has_llm_key:
            raise RuntimeError(
                "No LLM key configured. Set GROQ_API_KEY (free: https://console.groq.com) "
                "or OPENAI_API_KEY in .env, or run with LLM_MODE=demo for the offline brain."
            )
        # An explicit `model` (e.g. --model) pins the brain to one model with no
        # fallback; otherwise the configured primary leads the fallback chain.
        self._chain: list[str] = [model] if model else list(config.llm_models)
        self._index = 0
        self._prefix = (
            "groq"
            if config.groq_api_key
            else ("openai-compatible" if config.openai_api_key else "none")
        )
        self.model = self._chain[0]
        self.provider = f"{self._prefix}:{self.model}"

    def _advance(self) -> bool:
        """Switch to the next model in the fallback chain. False when exhausted."""
        previous = self.model
        self._index += 1
        if self._index >= len(self._chain):
            return False
        self.model = self._chain[self._index]
        self.provider = f"{self._prefix}:{self.model}"
        print(
            f"  [llm] '{previous}' hit its daily cap/unavailable -> switching to '{self.model}'",
            flush=True,
        )
        return True

    def _payload(self, messages: list[dict], tools: list[dict]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.1,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        # reasoning models on Groq burn TPM on hidden reasoning tokens; keep it low
        # unless the operator asks otherwise. Only sent for models that accept it.
        if config.llm_reasoning_effort and "gpt-oss" in self.model:
            payload["reasoning_effort"] = config.llm_reasoning_effort
        return payload

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        history: list[dict] | None = None,
        force_answer: bool = False,
    ) -> LLMResponse:
        if force_answer:
            # Last chance: no tools, just the verdict, so a model that likes to
            # keep digging still has to produce the report.
            messages = messages + [
                {
                    "role": "user",
                    "content": (
                        "Stop investigating. Using only the evidence already gathered above, "
                        "write the final report now in the required format. Do not call any tools."
                    ),
                }
            ]
            tools = []
        response = None
        attempt = 0
        while attempt < self.MAX_RETRIES:
            attempt += 1
            # Rebuilt each attempt: an earlier one may have switched model.
            payload = self._payload(messages, tools)
            try:
                response = requests.post(
                    f"{config.llm_base_url}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {config.llm_api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=180,
                )
            except requests.RequestException as exc:
                if attempt == self.MAX_RETRIES:
                    raise RuntimeError(f"LLM request failed after {attempt} attempts: {exc}") from exc
                time.sleep(min(2 ** attempt, 30))
                continue

            # Daily cap (TPD) or a dropped model (404): walk the fallback chain
            # to the next model instead of stalling. Free tiers cap tokens
            # *per model*, so the next model usually has its own fresh budget.
            if response.status_code == 404 or (
                response.status_code == 429 and _is_daily_cap(response)
            ):
                if self._advance():
                    attempt = 0  # fresh retry budget for the new model
                    continue
                reason = (
                    "daily token cap"
                    if (response.status_code == 429 and _is_daily_cap(response))
                    else "model unavailable (404)"
                )
                raise RuntimeError(
                    f"No usable model left — {reason} on every model tried: "
                    f"{', '.join(self._chain)}.\n"
                    "  * Run the demo with --offline for the full graph investigation now, or\n"
                    "  * wait for the daily limit to reset, or add a model to LLM_MODELS in .env.\n"
                    f"  API said: {(response.text or '')[:160]}"
                ) from None
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt == self.MAX_RETRIES:
                    raise RuntimeError(
                        f"LLM request to {config.llm_provider} failed ({response.status_code}) "
                        f"after {attempt} attempts: {response.text[:400]}"
                    )
                time.sleep(_retry_delay(response, attempt))
                continue

            if response.status_code >= 400:
                raise RuntimeError(
                    f"LLM request to {config.llm_provider} failed ({response.status_code}): "
                    f"{response.text[:500]}"
                )
            break

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
    """Deterministic brain for the offline fallback (no API key available).

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

    def chat(
        self,
        messages: list[dict],
        tools: list[dict],
        history: list[dict] | None = None,
        force_answer: bool = False,
    ) -> LLMResponse:
        history = history or []
        question = _last_user_message(messages)

        if force_answer:
            return LLMResponse(content=self._on_finish(question, history))

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


def _is_daily_cap(response: Any) -> bool:
    """True when a 429 is Groq's per-day token cap (TPD), not a burst limit.

    The daily cap needs a wait measured in minutes/hours, so the retry loop
    should give up immediately rather than spin.
    """
    text = (getattr(response, "text", "") or "").lower()
    return ("per day" in text) or ("tokens per day" in text) or ("tpd" in text)


def _retry_delay(response: Any, attempt: int) -> float:
    """How long to wait before retrying, preferring what the API tells us.

    Groq's 429 body says "Please try again in 5.9025s"; honour that (plus a
    small margin), else fall back to exponential backoff capped at 30s.
    """
    header = response.headers.get("retry-after") if hasattr(response, "headers") else None
    if header:
        try:
            return min(float(header) + 1.0, 60.0)
        except ValueError:
            pass
    match = re.search(r"try again in ([\d.]+)\s*s", response.text or "")
    if match:
        return min(float(match.group(1)) + 1.5, 60.0)
    return min(2.0 * attempt, 30.0)


def get_brain(scripted: ScriptedBrain | None = None, model: str | None = None):
    """Pick the brain: live model when a key is configured, else the scripted one.

    `model` pins the live brain to one specific model (no fallback chain).
    """
    if config.use_live_llm():
        return LiveBrain(model=model)
    if scripted is None:
        raise RuntimeError(
            "Offline fallback needs a scripted brain, but none was provided. "
            "Set GROQ_API_KEY or OPENAI_API_KEY in .env to use a real model."
        )
    return scripted
