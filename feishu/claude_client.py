"""
Claude API client for the Feishu bot.
Sends user messages to Claude and returns the reply text.
API key, base URL, and model are read from ~/.claude/settings.json (env) when present.
"""
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Load feishu/.env when running from project root (for FEISHU_* only; Claude uses settings.json)
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)

logger = logging.getLogger(__name__)

SETTINGS_PATH = Path(os.path.expanduser("~/.claude/settings.json"))
DEFAULT_BASE_URL = "https://api.anthropic.com"
DEFAULT_MODEL = "claude-3-5-sonnet-20241022"


def _load_settings_env():
    """Load env dict from ~/.claude/settings.json. Returns {} if file missing or invalid."""
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with open(SETTINGS_PATH, "r") as f:
            data = json.load(f)
        return data.get("env") or {}
    except Exception as e:
        logger.debug("Could not load %s: %s", SETTINGS_PATH, e)
        return {}


def _get_client():
    """Build Anthropic client. Prefer ~/.claude/settings.json env over os.environ."""
    settings = _load_settings_env()
    api_key = (
        settings.get("ANTHROPIC_AUTH_TOKEN")
        or settings.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or ""
    ).strip()
    base_url = (
        settings.get("ANTHROPIC_BASE_URL")
        or os.environ.get("ANTHROPIC_BASE_URL")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    if not api_key:
        raise ValueError(
            "No ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN. "
            "Set env in ~/.claude/settings.json or in feishu/.env."
        )
    import anthropic
    kwargs = {"api_key": api_key}
    if base_url != DEFAULT_BASE_URL:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs), base_url


def ask_claude(user_text: str, *, model: str | None = None, system: str | None = None) -> str:
    """
    Send the user message to Claude and return the assistant reply as a single string.
    Raises on missing API key or API errors.
    """
    settings = _load_settings_env()
    client, _ = _get_client()
    model = (
        model
        or settings.get("ANTHROPIC_MODEL")
        or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL)
    )
    kwargs = {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": user_text}],
    }
    if system:
        kwargs["system"] = system
    try:
        message = client.messages.create(**kwargs)
    except Exception as e:
        logger.exception("Claude API error: %s", e)
        raise
    parts = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", "") or "")
    return "\n".join(parts).strip() if parts else ""
