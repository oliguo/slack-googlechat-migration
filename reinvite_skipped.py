"""Re-invite previously skipped users to their Google Chat spaces.

Reads migration_state.json and finds all "skipped:<channel>" entries.
Attempts to invite those users again — useful after they've been added
to Google Workspace.

Usage:
    python reinvite_skipped.py          # dry-run (default)
    python reinvite_skipped.py --apply  # actually send invites
"""

import json
import logging
import sys
from pathlib import Path

import config
from gchat_client import GoogleChatImporter

STATE_FILE = Path("migration_state.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("reinvite")


def main() -> None:
    dry_run = "--apply" not in sys.argv

    if not STATE_FILE.exists():
        sys.exit(f"ERROR: {STATE_FILE} not found. Run migrate.py first.")

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    migrated = state.get("migrated_channels", {})

    # Collect all skipped entries: channel_name → list of emails.
    to_reinvite: dict[str, list[str]] = {}
    for key, emails in state.items():
        if not key.startswith("skipped:") or not emails:
            continue
        channel_name = key[len("skipped:"):]
        if channel_name not in migrated:
            logger.warning("Channel #%s has skipped users but is not in migrated_channels — skipping.", channel_name)
            continue
        to_reinvite[channel_name] = list(emails)

    if not to_reinvite:
        logger.info("No skipped users to re-invite.")
        return

    total_users = sum(len(v) for v in to_reinvite.values())
    logger.info("Found %d skipped user(s) across %d channel(s):",
                total_users, len(to_reinvite))
    for ch, emails in to_reinvite.items():
        logger.info("  #%s: %s", ch, ", ".join(emails))

    if dry_run:
        logger.info("")
        logger.info("DRY RUN — no invites will be sent.")
        logger.info("Run with --apply to actually invite.")
        return

    gchat = GoogleChatImporter(
        auth_mode=config.GOOGLE_AUTH_MODE,
        client_secret_path=config.GOOGLE_OAUTH_CLIENT_SECRET_PATH,
        service_account_key_path=config.GOOGLE_SERVICE_ACCOUNT_KEY_PATH,
        delegated_user_email=config.GOOGLE_DELEGATED_USER_EMAIL,
        rate_limit=config.GCHAT_RATE_LIMIT,
    )

    total_added = 0
    total_still_skipped = 0
    total_failed = 0

    for channel_name, emails in to_reinvite.items():
        space_name = migrated[channel_name].get("space_name")
        if not space_name:
            logger.warning("  No space_name for #%s — skipping.", channel_name)
            continue

        invited_key = f"invited:{channel_name}"
        skipped_key = f"skipped:{channel_name}"
        already_invited: set[str] = set(state.get(invited_key, []))
        still_skipped: list[str] = []

        logger.info("Re-inviting %d user(s) to #%s (space %s) …",
                     len(emails), channel_name, space_name)

        for email in emails:
            result = gchat.invite_member(space_name, email)
            if result == "added":
                logger.info("  ✓ Invited %s", email)
                already_invited.add(email)
                total_added += 1
            elif result == "exists":
                logger.info("  ✓ %s already a member", email)
                already_invited.add(email)
                total_added += 1
            elif result == "not_found":
                logger.warning("  ⚠ Still not in Google Workspace: %s", email)
                still_skipped.append(email)
                total_still_skipped += 1
            else:
                logger.error("  ❌ Failed to invite %s", email)
                still_skipped.append(email)
                total_failed += 1

        # Update state: move successful invites out of skipped.
        state[invited_key] = list(already_invited)
        state[skipped_key] = still_skipped

    # Write updated state back.
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")

    logger.info("")
    logger.info("=" * 50)
    logger.info("Re-invite complete!")
    logger.info("  Added / already member:  %d", total_added)
    logger.info("  Still not in workspace:  %d", total_still_skipped)
    logger.info("  Failed:                  %d", total_failed)
    logger.info("=" * 50)


if __name__ == "__main__":
    main()
