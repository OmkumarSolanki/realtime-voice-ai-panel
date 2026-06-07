"""Configuration for The Panel backend.

Loads keys/settings from the repo-root .env file (one directory above /backend).
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# .env lives at the repo root. This file is backend/panel/config.py, so the repo
# root is two directories up.
REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")


class Config:
    """Runtime configuration, read once at import time."""

    OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")
    DEEPGRAM_API_KEY: str = os.environ.get("DEEPGRAM_API_KEY", "")
    CARTESIA_API_KEY: str = os.environ.get("CARTESIA_API_KEY", "")

    LLM_MODEL: str = os.environ.get("LLM_MODEL", "gpt-4o")

    # Cartesia low-latency speech model. Override via env if your account differs.
    CARTESIA_MODEL: str = os.environ.get("CARTESIA_MODEL", "sonic-2")
    CARTESIA_VERSION: str = os.environ.get("CARTESIA_VERSION", "2025-04-16")

    NUM_EXPERTS: int = int(os.environ.get("NUM_EXPERTS", "3"))
    PORT: int = int(os.environ.get("PORT", "8000"))

    # Audio sample rate used end-to-end.
    SAMPLE_RATE: int = 16000

    @classmethod
    def missing_keys(cls) -> list[str]:
        """Return the names of required keys that are not set."""
        missing = []
        if not cls.OPENAI_API_KEY:
            missing.append("OPENAI_API_KEY")
        if not cls.DEEPGRAM_API_KEY:
            missing.append("DEEPGRAM_API_KEY")
        if not cls.CARTESIA_API_KEY:
            missing.append("CARTESIA_API_KEY")
        return missing


config = Config()
