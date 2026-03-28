"""Configuration loader — reads .env and exposes validated settings."""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        sys.exit(f"ERROR: Required environment variable '{key}' is not set. "
                 f"Copy .env.example to .env and fill in the values.")
    return value


# Slack
SLACK_BOT_TOKEN: str = _require("SLACK_BOT_TOKEN")
SLACK_CHANNELS_RAW: str = os.getenv("SLACK_CHANNELS", "").strip()
SLACK_CHANNELS: list[str] | None = (
    [ch.strip() for ch in SLACK_CHANNELS_RAW.split(",") if ch.strip()]
    if SLACK_CHANNELS_RAW
    else None
)

# Google Chat — authentication mode
GOOGLE_AUTH_MODE: str = os.getenv("GOOGLE_AUTH_MODE", "oauth").strip().lower()

if GOOGLE_AUTH_MODE == "service_account":
    GOOGLE_SERVICE_ACCOUNT_KEY_PATH: str = _require("GOOGLE_SERVICE_ACCOUNT_KEY_PATH")
    GOOGLE_DELEGATED_USER_EMAIL: str = _require("GOOGLE_DELEGATED_USER_EMAIL")
    _key_path = Path(GOOGLE_SERVICE_ACCOUNT_KEY_PATH)
    if not _key_path.exists():
        sys.exit(
            f"ERROR: Google service account key file not found at "
            f"'{GOOGLE_SERVICE_ACCOUNT_KEY_PATH}'."
        )
    GOOGLE_OAUTH_CLIENT_SECRET_PATH: str = ""
else:
    GOOGLE_OAUTH_CLIENT_SECRET_PATH: str = _require("GOOGLE_OAUTH_CLIENT_SECRET_PATH")
    _cs_path = Path(GOOGLE_OAUTH_CLIENT_SECRET_PATH)
    if not _cs_path.exists():
        sys.exit(
            f"ERROR: OAuth client secret file not found at "
            f"'{GOOGLE_OAUTH_CLIENT_SECRET_PATH}'. "
            f"Download it from Google Cloud Console → APIs & Services → Credentials."
        )
    GOOGLE_SERVICE_ACCOUNT_KEY_PATH: str = ""
    GOOGLE_DELEGATED_USER_EMAIL: str = ""

# Migration options
SPACE_NAME_PREFIX: str = os.getenv("SPACE_NAME_PREFIX", "[Migrated]").strip()
GCHAT_RATE_LIMIT: int = int(os.getenv("GCHAT_RATE_LIMIT", "2"))
INCLUDE_FILE_LINKS: bool = os.getenv("INCLUDE_FILE_LINKS", "true").lower() == "true"
INVITE_MEMBERS: bool = os.getenv("INVITE_MEMBERS", "true").lower() == "true"
ARCHIVE_AFTER_MIGRATE: bool = os.getenv("ARCHIVE_AFTER_MIGRATE", "false").lower() == "true"
SLACK_USER_TOKEN: str = os.getenv("SLACK_USER_TOKEN", "").strip()
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
