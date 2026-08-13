"""Configuration loaded from environment variables.

Every credential is read here and nowhere else. Missing required values make
the process exit immediately with a clear message instead of silently no-oping.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from dotenv import load_dotenv

REQUIRED_VARS = {
    "ANTHROPIC_API_KEY": "Anthropic API key used to draft posts",
    "TELEGRAM_BOT_TOKEN": "Telegram bot token from @BotFather",
    "TELEGRAM_CHAT_ID": "numeric id of the Telegram chat that receives drafts",
    "BUFFER_ACCESS_TOKEN": "Buffer API access token",
    "BUFFER_PROFILE_ID": "id of the connected X profile in Buffer",
}


@dataclass(frozen=True)
class Config:
    anthropic_api_key: str
    telegram_bot_token: str
    telegram_chat_id: int
    buffer_access_token: str
    buffer_profile_id: str

    github_token: str
    github_repo: str
    zenodo_community: str
    check_interval_minutes: int
    anthropic_model: str
    db_path: str
    process_backlog_on_first_run: bool
    max_items_per_cycle: int


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"Config error: {name} must be an integer, got {raw!r}", file=sys.stderr)
        sys.exit(1)


def load_config() -> Config:
    load_dotenv()

    missing = [name for name in REQUIRED_VARS if not os.environ.get(name, "").strip()]
    if missing:
        print("Refusing to start: missing required environment variables.", file=sys.stderr)
        for name in missing:
            print(f"  - {name}: {REQUIRED_VARS[name]}", file=sys.stderr)
        print("See .env.example for the full list.", file=sys.stderr)
        sys.exit(1)

    chat_id_raw = os.environ["TELEGRAM_CHAT_ID"].strip()
    try:
        chat_id = int(chat_id_raw)
    except ValueError:
        print(
            f"Config error: TELEGRAM_CHAT_ID must be a numeric chat id, got {chat_id_raw!r}. "
            "Send /id to your bot to discover it.",
            file=sys.stderr,
        )
        sys.exit(1)

    return Config(
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"].strip(),
        telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
        telegram_chat_id=chat_id,
        buffer_access_token=os.environ["BUFFER_ACCESS_TOKEN"].strip(),
        buffer_profile_id=os.environ["BUFFER_PROFILE_ID"].strip(),
        github_token=os.environ.get("GITHUB_TOKEN", "").strip(),
        github_repo=os.environ.get("GITHUB_REPO", "").strip() or "hasan-mavlonov/mindform_v0",
        zenodo_community=os.environ.get("ZENODO_COMMUNITY", "").strip() or "mindform-ai-research",
        check_interval_minutes=_int_env("CHECK_INTERVAL_MINUTES", 180),
        anthropic_model=os.environ.get("ANTHROPIC_MODEL", "").strip() or "claude-opus-5",
        db_path=os.environ.get("DB_PATH", "").strip() or "posting_automation.db",
        process_backlog_on_first_run=_bool_env("PROCESS_BACKLOG_ON_FIRST_RUN", False),
        max_items_per_cycle=_int_env("MAX_ITEMS_PER_CYCLE", 10),
    )
