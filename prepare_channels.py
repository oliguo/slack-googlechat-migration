"""Prepare Slack channels for migration.

This script uses a **Slack User OAuth Token** (xoxp-…) to:
1. List ALL channels: public, private, and archived
2. Unarchive any archived channels
3. Invite the bot to every channel

Usage:
    python prepare_channels.py

Requires these environment variables in .env:
    SLACK_USER_TOKEN   — A User OAuth Token (xoxp-…) from an admin user
    SLACK_BOT_TOKEN    — The Bot User OAuth Token (xoxb-…) already in .env
    SLACK_BOT_USER_ID  — The bot's Slack User ID (e.g. U0123456789)
"""

import logging
import os
import sys
import time

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("prepare")


def _require(key: str) -> str:
    value = os.getenv(key, "").strip()
    if not value:
        sys.exit(f"ERROR: Required env var '{key}' is not set. Add it to .env")
    return value


def _api_call(method, **kwargs):
    """Call a Slack API method with automatic rate-limit retry."""
    while True:
        try:
            return method(**kwargs)
        except SlackApiError as exc:
            if exc.response.status_code == 429:
                retry_after = int(exc.response.headers.get("Retry-After", 5))
                logger.warning("Rate-limited — waiting %ds …", retry_after)
                time.sleep(retry_after)
                continue
            raise


def fetch_all_channels(client: WebClient) -> list[dict]:
    """Fetch all public, private, and archived channels."""
    channels: list[dict] = []
    for types in ("public_channel", "private_channel"):
        cursor = None
        while True:
            resp = _api_call(
                client.conversations_list,
                types=types,
                exclude_archived=False,  # Include archived channels
                limit=200,
                cursor=cursor,
            )
            channels.extend(resp["channels"])
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    return channels


def unarchive_channel(client: WebClient, channel: dict) -> bool:
    """Unarchive a channel. Returns True if unarchived, False if skipped."""
    if not channel.get("is_archived"):
        return False
    try:
        _api_call(client.conversations_unarchive, channel=channel["id"])
        logger.info("  📦 Unarchived #%s", channel["name"])
        return True
    except SlackApiError as exc:
        if exc.response["error"] == "not_archived":
            return False
        logger.error("  ❌ Failed to unarchive #%s: %s", channel["name"], exc.response["error"])
        return False


def invite_bot(client: WebClient, channel: dict, bot_user_id: str) -> bool:
    """Invite the bot user to a channel. Returns True if newly invited."""
    try:
        _api_call(
            client.conversations_invite,
            channel=channel["id"],
            users=bot_user_id,
        )
        logger.info("  🤖 Invited bot to #%s", channel["name"])
        return True
    except SlackApiError as exc:
        error = exc.response["error"]
        if error == "already_in_channel":
            logger.debug("  ✓ Bot already in #%s", channel["name"])
            return False
        if error == "cant_invite_self":
            logger.debug("  ✓ Bot is self for #%s", channel["name"])
            return False
        logger.error("  ❌ Failed to invite bot to #%s: %s", channel["name"], error)
        return False


def main() -> None:
    user_token = _require("SLACK_USER_TOKEN")
    bot_user_id = _require("SLACK_BOT_USER_ID")

    user_client = WebClient(token=user_token)

    logger.info("=" * 60)
    logger.info("Slack Channel Preparation")
    logger.info("=" * 60)

    # --- Fetch all channels ---
    logger.info("Fetching all channels (public, private, archived) …")
    channels = fetch_all_channels(user_client)

    public = [c for c in channels if not c.get("is_private")]
    private = [c for c in channels if c.get("is_private")]
    archived = [c for c in channels if c.get("is_archived")]

    logger.info("Found %d channels total:", len(channels))
    logger.info("  Public:   %d", len(public))
    logger.info("  Private:  %d", len(private))
    logger.info("  Archived: %d", len(archived))

    # --- Unarchive archived channels ---
    unarchived_count = 0
    if archived:
        logger.info("")
        logger.info("Unarchiving %d archived channel(s) …", len(archived))
        for ch in archived:
            if unarchive_channel(user_client, ch):
                unarchived_count += 1

    # --- Invite bot to all channels ---
    logger.info("")
    logger.info("Inviting bot (%s) to all %d channel(s) …", bot_user_id, len(channels))
    invited_count = 0
    already_count = 0
    for idx, ch in enumerate(channels, start=1):
        if invite_bot(user_client, ch, bot_user_id):
            invited_count += 1
        else:
            already_count += 1
        if idx % 50 == 0:
            logger.info("  → Progress: %d/%d channels", idx, len(channels))

    # --- Summary ---
    logger.info("")
    logger.info("=" * 60)
    logger.info("Done!")
    logger.info("  Channels found:     %d", len(channels))
    logger.info("  Unarchived:         %d", unarchived_count)
    logger.info("  Bot newly invited:  %d", invited_count)
    logger.info("  Bot already in:     %d", already_count)
    logger.info("=" * 60)
    logger.info("")
    logger.info("You can now run the migration with all channels:")
    logger.info("  1. Set SLACK_CHANNELS= (empty) in .env to migrate all")
    logger.info("  2. Run: python migrate.py")


if __name__ == "__main__":
    main()
