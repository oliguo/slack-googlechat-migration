"""Archive Slack channels that have been fully migrated.

Reads migration_state.json and archives every channel listed under
"migrated_channels".  Uses SLACK_USER_TOKEN (xoxp-…) which needs
channels:manage + groups:write scopes.

Usage:
    python archive_migrated.py          # dry-run (default)
    python archive_migrated.py --apply  # actually archive
"""

import json
import logging
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

STATE_FILE = Path("migration_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("archive")


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


def _resolve_channel_ids(client: WebClient, channel_names: list[str]) -> dict[str, str]:
    """Map channel names → channel IDs by listing all conversations."""
    name_to_id: dict[str, str] = {}
    remaining = set(channel_names)

    for channel_type in ("public_channel", "private_channel"):
        cursor = None
        while remaining:
            resp = _api_call(
                client.conversations_list,
                types=channel_type,
                exclude_archived=False,
                limit=200,
                cursor=cursor,
            )
            for ch in resp["channels"]:
                name = ch.get("name", "")
                if name in remaining:
                    name_to_id[name] = ch["id"]
                    remaining.discard(name)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

    return name_to_id


def main() -> None:
    dry_run = "--apply" not in sys.argv

    # Prefer user token (has manage scopes); fall back to bot token.
    token = os.getenv("SLACK_USER_TOKEN", "").strip() or os.getenv("SLACK_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("ERROR: Set SLACK_USER_TOKEN or SLACK_BOT_TOKEN in .env")

    if not STATE_FILE.exists():
        sys.exit(f"ERROR: {STATE_FILE} not found. Run migrate.py first.")

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    migrated = state.get("migrated_channels", {})

    if not migrated:
        logger.info("No migrated channels found in %s.", STATE_FILE)
        return

    channel_names = list(migrated.keys())
    logger.info("Found %d migrated channel(s): %s",
                len(channel_names), ", ".join(f"#{n}" for n in channel_names))

    if dry_run:
        logger.info("")
        logger.info("DRY RUN — no channels will be archived.")
        logger.info("Run with --apply to actually archive.")
        logger.info("")

    client = WebClient(token=token)

    # Resolve names to IDs.
    logger.info("Resolving channel IDs …")
    name_to_id = _resolve_channel_ids(client, channel_names)

    archived_count = 0
    skipped_count = 0
    failed_count = 0

    for name in channel_names:
        channel_id = name_to_id.get(name)
        if not channel_id:
            logger.warning("  ⚠ Could not find channel #%s — skipping", name)
            skipped_count += 1
            continue

        if dry_run:
            logger.info("  [DRY RUN] Would archive #%s (%s)", name, channel_id)
            archived_count += 1
            continue

        try:
            _api_call(client.conversations_archive, channel=channel_id)
            logger.info("  ✓ Archived #%s", name)
            archived_count += 1
        except SlackApiError as exc:
            error = exc.response.get("error", "")
            if error == "already_archived":
                logger.info("  ✓ #%s already archived", name)
                archived_count += 1
            else:
                logger.error("  ❌ Failed to archive #%s: %s", name, error)
                failed_count += 1

    logger.info("")
    logger.info("=" * 50)
    logger.info("Done%s!", " (DRY RUN)" if dry_run else "")
    logger.info("  Archived:  %d", archived_count)
    logger.info("  Skipped:   %d", skipped_count)
    logger.info("  Failed:    %d", failed_count)
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
