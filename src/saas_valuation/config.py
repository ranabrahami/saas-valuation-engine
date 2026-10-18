"""Loads config/config.yaml and secrets from .env.

Every other module gets its settings from here, so no assumption is
hard-coded anywhere else in the codebase.
"""

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"


def load_config(path: Path = CONFIG_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def universe_tickers(cfg: dict) -> list[str]:
    """Flatten the bucketed universe into a single ticker list."""
    return [t for group in cfg["universe"].values() for t in group]


def ticker_buckets(cfg: dict) -> dict[str, str]:
    """Map each ticker to its profile bucket, e.g. {"ADBE": "mature_profitable"}."""
    return {t: bucket for bucket, group in cfg["universe"].items() for t in group}


def project_path(relative: str) -> Path:
    return PROJECT_ROOT / relative


def sec_user_agent(cfg: dict) -> str:
    """Read the SEC User-Agent from .env; fail loudly if it's missing.

    The SEC blocks anonymous requests, so there is no sensible fallback.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    var = cfg["data"]["user_agent_env_var"]
    value = os.getenv(var, "").strip()
    if not value or "example.com" in value:
        raise RuntimeError(
            f"{var} is not set. Copy .env.example to .env and add your name and email."
        )
    return value
