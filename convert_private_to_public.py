"""Convert private Slack channels to public channels.

Slack's standard export (from the admin console) only includes public
channels.  This script converts private channels to public so that a
full export contains all channel data.

Usage:
    python convert_private_to_public.py              # Dry-run (list only)
    python convert_private_to_public.py --apply      # Actually convert

Requires SLACK_USER_TOKEN (xoxp-…) from a Workspace Owner / Admin
with channels:read, groups:read, and channels:manage scopes.
"""

import argparse
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
logger = logging.getLogger("convert_channels")


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


def fetch_private_channels(client: WebClient) -> list[dict]:
    """Fetch all private channels (including archived)."""
    channels: list[dict] = []
    cursor = None
    while True:
        resp = _api_call(
            client.conversations_list,
            types="private_channel",
            exclude_archived=False,
            limit=200,
            cursor=cursor,
        )
        channels.extend(resp["channels"])
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return channels


def convert_to_public(client: WebClient, channel: dict) -> bool:
    """Convert a private channel to public using admin.conversations.convertToPublic.

    Returns True on success, False on failure.
    """
    try:
        # admin.conversations.convertToPublic requires an admin/owner user token
        _api_call(
            client.admin_conversations_convertToPublic,
            channel_id=channel["id"],
        )
        logger.info("  ✅ Converted #%s to public", channel["name"])
        return True
    except SlackApiError as exc:
        error = exc.response.get("error", "unknown_error")
        if error == "already_public":
            logger.info("  ⏭  #%s is already public", channel["name"])
            return True
        if error in ("missing_scope", "not_allowed_token_type", "restricted_action",
                      "not_an_admin", "not_an_enterprise"):
            logger.error(
                "  ❌ Cannot convert #%s: '%s'. "
                "Your token needs admin.conversations:write scope and you must be a Workspace Owner.",
                channel["name"], error,
            )
            return False
        logger.error("  ❌ Failed to convert #%s: %s", channel["name"], error)
        return False


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert private Slack channels to public for full data export."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually convert channels. Without this flag, only lists them (dry-run).",
    )
    parser.add_argument(
        "--channels", type=str, default="",
        help="Comma-separated list of channel names to convert. If omitted, converts ALL private channels.",
    )
    args = parser.parse_args()

    user_token = _require("SLACK_USER_TOKEN")
    client = WebClient(token=user_token)

    logger.info("=" * 60)
    logger.info("Slack: Convert Private Channels → Public")
    logger.info("=" * 60)

    # Fetch all private channels.
    logger.info("Fetching private channels …")
    private_channels = fetch_private_channels(client)

    if not private_channels:
        logger.info("No private channels found. Nothing to do.")
        return

    # Optionally filter to specific channels.
    filter_names: set[str] | None = None
    if args.channels:
        filter_names = {n.strip().lstrip("#") for n in args.channels.split(",") if n.strip()}

    targets = [
        ch for ch in private_channels
        if filter_names is None or ch["name"] in filter_names
    ]

    if not targets:
        logger.info("No matching private channels found for the given filter.")
        return

    logger.info("Found %d private channel(s) to convert:", len(targets))
    for ch in targets:
        archived_tag = " (archived)" if ch.get("is_archived") else ""
        logger.info("  • #%s%s", ch["name"], archived_tag)

    if not args.apply:
        logger.info("")
        logger.info("DRY RUN — no changes made. Re-run with --apply to convert.")
        return

    # Convert each channel.
    logger.info("")
    logger.info("Converting %d channel(s) …", len(targets))
    success = 0
    failed = 0
    for idx, ch in enumerate(targets, start=1):
        if convert_to_public(client, ch):
            success += 1
        else:
            failed += 1
        if idx % 20 == 0:
            logger.info("  → Progress: %d/%d", idx, len(targets))

    # Summary.
    logger.info("")
    logger.info("=" * 60)
    logger.info("Done! Converted: %d, Failed: %d", success, failed)
    if failed:
        logger.warning(
            "Some channels could not be converted. Check errors above."
        )
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
