"""Check external user access on Google Chat spaces.

The `externalUserAllowed` field is IMMUTABLE — it can only be set when
a space is first created and cannot be changed via the API afterward.

This script checks which migrated spaces already allow external users
and which ones need to be updated manually in the Google Chat UI.

For spaces that need updating:
  1. Open the space in Google Chat
  2. Click the space name → Space settings (or manage members)
  3. Enable "People outside of your organization can join"

Future spaces created by migrate.py will automatically have this enabled.

Usage:
    python enable_external_access.py              # check all migrated spaces
    python enable_external_access.py web-tomohk   # check specific channels
"""

import json
import logging
import sys
import time
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

STATE_FILE = Path("migration_state.json")
TOKEN_PATH = Path("token.json")

OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.spaces.create",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.memberships",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("external_access")


def _get_credentials() -> Credentials:
    creds: Credentials | None = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), OAUTH_SCOPES)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif not creds or not creds.valid:
        flow = InstalledAppFlow.from_client_secrets_file(
            config.GOOGLE_OAUTH_CLIENT_SECRET_PATH, OAUTH_SCOPES
        )
        creds = flow.run_local_server(port=0)
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


def main() -> None:
    args = [a for a in sys.argv[1:]]

    if not STATE_FILE.exists():
        sys.exit(f"ERROR: {STATE_FILE} not found. Run migrate.py first.")

    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    migrated = state.get("migrated_channels", {})

    if not migrated:
        logger.info("No migrated channels found in %s.", STATE_FILE)
        return

    # Filter to specific channels if provided, otherwise all.
    if args:
        targets = {name: info for name, info in migrated.items() if name in args}
        not_found = [a for a in args if a not in migrated]
        if not_found:
            logger.warning("Channels not found in migrated list: %s", ", ".join(f"#{n}" for n in not_found))
    else:
        targets = migrated

    if not targets:
        logger.info("No matching channels to check.")
        return

    logger.info("Checking %d space(s) for external user access:", len(targets))

    creds = _get_credentials()
    service = build("chat", "v1", credentials=creds)

    enabled: list[str] = []
    needs_manual: list[tuple[str, str]] = []  # (channel_name, space_url)
    failed: list[str] = []

    for channel_name, info in targets.items():
        space_name = info.get("space_name")
        if not space_name:
            logger.warning("  No space_name for #%s — skipping.", channel_name)
            failed.append(channel_name)
            continue

        try:
            space = service.spaces().get(name=space_name).execute()
        except HttpError as exc:
            if exc.resp.status == 429:
                retry_after = int(exc.resp.get("retry-after", 5))
                time.sleep(retry_after)
                try:
                    space = service.spaces().get(name=space_name).execute()
                except HttpError:
                    logger.error("  ❌ Failed to get #%s (%s)", channel_name, space_name)
                    failed.append(channel_name)
                    continue
            else:
                logger.error("  ❌ Failed to get #%s (%s): HTTP %d", channel_name, space_name, exc.resp.status)
                failed.append(channel_name)
                continue

        if space.get("externalUserAllowed"):
            logger.info("  ✓ #%s — external access already enabled", channel_name)
            enabled.append(channel_name)
        else:
            space_id = space_name.split("/")[-1]
            space_url = f"https://mail.google.com/mail/u/0/#chat/space/{space_id}"
            logger.warning("  ⚠ #%s — external access NOT enabled (must fix manually)", channel_name)
            needs_manual.append((channel_name, space_url))

    logger.info("")
    logger.info("=" * 60)
    logger.info("Results:")
    logger.info("  Already enabled:     %d", len(enabled))
    logger.info("  Needs manual update: %d", len(needs_manual))
    logger.info("  Failed to check:     %d", len(failed))
    logger.info("=" * 60)

    if needs_manual:
        logger.info("")
        logger.info("The following spaces need manual update in Google Chat UI:")
        logger.info("(Open each link → Space settings → Enable external access)")
        logger.info("")
        for name, url in needs_manual:
            logger.info("  #%-30s  %s", name, url)

        logger.info("")
        logger.info("NOTE: 'externalUserAllowed' is immutable in the API — it can only")
        logger.info("be set at space creation time. Future spaces created by migrate.py")
        logger.info("will automatically have external access enabled.")


if __name__ == "__main__":
    main()
