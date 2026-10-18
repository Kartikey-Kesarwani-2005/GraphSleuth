"""Configuration for GraphSleuth, loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # .env is optional — plain environment variables work too
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # pragma: no cover - dotenv is a convenience only
    pass


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


@dataclass
class Config:
    """Runtime configuration. Every value can be overridden from the environment."""

    falkordb_host: str = _env("FALKORDB_HOST", "localhost")
    falkordb_port: int = int(_env("FALKORDB_PORT", "6379") or 6379)
    falkordb_password: str = _env("FALKORDB_PASSWORD")
    graph_name: str = _env("FALKORDB_GRAPH", "graphsleuth")

    # Groq (https://console.groq.com) — OpenAI-compatible, free tier.
    groq_api_key: str = _env("GROQ_API_KEY")
    groq_base_url: str = _env("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    groq_model: str = _env("GROQ_MODEL", "openai/gpt-oss-120b")

    # Any other OpenAI-compatible endpoint (OpenRouter, Gemini, Ollama, ...).
    openai_api_key: str = _env("OPENAI_API_KEY")
    openai_base_url: str = _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    openai_model: str = _env("OPENAI_MODEL", "gpt-4o-mini")

    llm_mode: str = _env("LLM_MODE", "auto").lower()  # auto | live | demo
    llm_reasoning_effort: str = _env("LLM_REASONING_EFFORT", "low")
    max_tool_rounds: int = int(_env("MAX_TOOL_ROUNDS", "20") or 20)

    # ------------------------------------------------------------------ #
    # Which endpoint the live brain talks to (GROQ_API_KEY wins if set).
    # ------------------------------------------------------------------ #
    @property
    def llm_api_key(self) -> str:
        return self.groq_api_key or self.openai_api_key

    @property
    def llm_base_url(self) -> str:
        return self.groq_base_url if self.groq_api_key else self.openai_base_url

    @property
    def llm_model(self) -> str:
        return self.groq_model if self.groq_api_key else self.openai_model

    @property
    def llm_models(self) -> list[str]:
        """Fallback chain of live models, primary first.

        Free tiers cap tokens **per model** per day, so when the current model
        runs dry the live brain walks this list instead of giving up. The
        configured primary (``GROQ_MODEL``/``OPENAI_MODEL``) is always tried
        first; ``LLM_MODELS`` (comma-separated) fills in the rest.
        """
        ordered: list[str] = []
        if self.llm_model:
            ordered.append(self.llm_model)
        for name in _env("LLM_MODELS").split(","):
            name = name.strip()
            if name and name not in ordered:
                ordered.append(name)
        return ordered or ["openai/gpt-oss-120b"]

    @property
    def llm_provider(self) -> str:
        if self.groq_api_key:
            return f"groq:{self.llm_model}"
        if self.openai_api_key:
            return f"openai-compatible:{self.openai_model}"
        return "none"

    @property
    def has_llm_key(self) -> bool:
        return bool(self.llm_api_key)

    def use_live_llm(self) -> bool:
        """Decide between the real LLM and the offline fallback brain."""
        if self.llm_mode == "demo":
            return False
        if self.llm_mode == "live":
            return True
        return self.has_llm_key


config = Config()
