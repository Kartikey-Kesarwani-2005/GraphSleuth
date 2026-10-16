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

    openai_api_key: str = _env("OPENAI_API_KEY")
    openai_base_url: str = _env("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    openai_model: str = _env("OPENAI_MODEL", "gpt-4o-mini")

    llm_mode: str = _env("LLM_MODE", "auto").lower()  # auto | live | demo
    max_tool_rounds: int = int(_env("MAX_TOOL_ROUNDS", "12") or 12)

    @property
    def has_llm_key(self) -> bool:
        return bool(self.openai_api_key)

    def use_live_llm(self) -> bool:
        """Decide between the real LLM and the scripted demo brain."""
        if self.llm_mode == "demo":
            return False
        if self.llm_mode == "live":
            return True
        return self.has_llm_key


config = Config()
