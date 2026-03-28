"""Main migration orchestrator — ties Slack export and Google Chat import together."""

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

import config
from slack_client import SlackExporter, SlackChannel
from gchat_client import GoogleChatImporter, format_slack_message

STATE_FILE = Path("migration_state.json")
SAVE_INTERVAL = 10  # Flush state to disk every N messages

logger = logging.getLogger("migrate")


# ------------------------------------------------------------------
# State persistence — allows resuming an interrupted migration
# ------------------------------------------------------------------

def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"migrated_channels": {}, "failed_channels": [], "in_progress": {}}


def _save_state(state: dict) -> None:
    """Atomically write state — write to temp file then rename to avoid corruption."""
    fd, tmp_path = tempfile.mkstemp(
        dir=STATE_FILE.parent, suffix=".tmp", prefix=".migration_state_"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_path, STATE_FILE)
    except BaseException:
        # Clean up temp file on failure.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------
# Slack channel archival
# ------------------------------------------------------------------

_archive_disabled = False  # Flipped to True on first permission error to skip further attempts.


def _archive_slack_channel(channel_id: str, channel_name: str, state: dict) -> None:
    """Archive a Slack channel after successful migration.

    Uses SLACK_USER_TOKEN (xoxp-) which needs channels:manage + groups:write
    scopes.  Falls back to SLACK_BOT_TOKEN if no user token is set.
    """
    global _archive_disabled
    if _archive_disabled:
        return

    archived_key = "archived_channels"
    already_archived: set[str] = set(state.get(archived_key, []))
    if channel_name in already_archived:
        logger.info("  ⏭  #%s already archived.", channel_name)
        return

    token = config.SLACK_USER_TOKEN or config.SLACK_BOT_TOKEN
    client = WebClient(token=token)

    for attempt in range(3):
        try:
            client.conversations_archive(channel=channel_id)
            logger.info("  🗄  Archived Slack channel #%s", channel_name)
            already_archived.add(channel_name)
            state[archived_key] = list(already_archived)
            _save_state(state)
            return
        except SlackApiError as exc:
            error = exc.response.get("error", "")
            if error == "already_archived":
                logger.info("  🗄  #%s was already archived.", channel_name)
                already_archived.add(channel_name)
                state[archived_key] = list(already_archived)
                _save_state(state)
                return
            if error in ("missing_scope", "restricted_action", "not_allowed_token_type"):
                logger.warning(
                    "  ⚠ Cannot archive channels: '%s'. "
                    "Set SLACK_USER_TOKEN to an xoxp- user token with channels:manage scope. "
                    "Disabling archive for the rest of this run.",
                    error,
                )
                _archive_disabled = True
                return
            if exc.response.status_code == 429:
                retry_after = int(exc.response.headers.get("Retry-After", 5))
                logger.warning("  Rate-limited archiving #%s — waiting %ds", channel_name, retry_after)
                time.sleep(retry_after)
                continue
            logger.error("  ⚠ Could not archive #%s: %s", channel_name, error)
            return


# ------------------------------------------------------------------
# Migration logic
# ------------------------------------------------------------------

def _migrate_channel(
    channel: SlackChannel,
    importer: GoogleChatImporter,
    state: dict,
) -> None:
    """Migrate a single Slack channel → Google Chat Space.

    Tracks progress per-message via state["in_progress"][channel_name] so
    that a re-run skips messages that were already posted successfully.
    """
    space_display_name = f"{config.SPACE_NAME_PREFIX} {channel.name}".strip()

    # Skip if already fully migrated.
    if channel.name in state["migrated_channels"]:
        logger.info("⏭  Skipping #%s (already migrated).", channel.name)
        return

    # Load or initialise per-channel progress.
    progress = state.setdefault("in_progress", {}).get(channel.name, {})
    space_name = progress.get("space_name") or importer.find_or_create_space(space_display_name)
    completed_ts: set[str] = set(progress.get("completed_ts", []))
    header_sent: bool = progress.get("header_sent", False)

    # Persist the space name immediately so a re-run reuses the same space.
    state.setdefault("in_progress", {})[channel.name] = {
        "space_name": space_name,
        "completed_ts": list(completed_ts),
        "header_sent": header_sent,
    }
    _save_state(state)

    # Post channel description as the first message (once).
    if not header_sent:
        description_parts = []
        if channel.topic:
            description_parts.append(f"*Topic:* {channel.topic}")
        if channel.purpose:
            description_parts.append(f"*Purpose:* {channel.purpose}")
        if description_parts:
            header = (
                f"📌 *Channel info for #{channel.name}*\n\n"
                + "\n".join(description_parts)
            )
            importer.post_message(space_name, header)
        state["in_progress"][channel.name]["header_sent"] = True
        _save_state(state)

    # Build a lookup: Slack thread_ts → list of replies.
    threads_by_ts: dict[str, tuple] = {}
    for thread in channel.threads:
        threads_by_ts[thread.parent.ts] = thread.replies

    migrated_count = len(completed_ts)
    total = len(channel.messages)

    if completed_ts:
        logger.info("  ↳ Resuming #%s — %d/%d already sent, continuing …",
                    channel.name, migrated_count, total)

    for idx, msg in enumerate(channel.messages, start=1):
        # Skip messages already posted in a previous run.
        if msg.ts in completed_ts:
            continue

        formatted = format_slack_message(
            user_name=msg.user_name,
            text=msg.text,
            ts=msg.ts,
            files=msg.files,
            reactions=msg.reactions,
            include_file_links=config.INCLUDE_FILE_LINKS,
        )
        gchat_thread_name = importer.post_message(space_name, formatted)
        migrated_count += 1

        # Post thread replies if any.
        replies = threads_by_ts.get(msg.ts, ())
        for reply in replies:
            try:
                reply_text = format_slack_message(
                    user_name=reply.user_name,
                    text=reply.text,
                    ts=reply.ts,
                    files=reply.files,
                    reactions=reply.reactions,
                    include_file_links=config.INCLUDE_FILE_LINKS,
                )
                importer.post_thread_reply(space_name, gchat_thread_name, reply_text)
            except Exception:
                logger.exception(
                    "  ⚠ Failed to post reply ts=%s in thread %s of #%s — skipping reply",
                    reply.ts, msg.ts, channel.name,
                )

        # Checkpoint this message as done.
        completed_ts.add(msg.ts)
        state["in_progress"][channel.name]["completed_ts"] = list(completed_ts)

        # Batch state saves to reduce I/O (always save the last message).
        if idx % SAVE_INTERVAL == 0 or idx == total:
            _save_state(state)

        if idx % 50 == 0 or idx == total:
            logger.info("  → Progress #%s: %d/%d messages", channel.name, migrated_count, total)

    # Mark fully complete and clean up in-progress entry.
    state["migrated_channels"][channel.name] = {
        "space_name": space_name,
        "message_count": migrated_count,
    }
    state["in_progress"].pop(channel.name, None)
    _save_state(state)
    logger.info("✅ Finished #%s — %d messages migrated.", channel.name, migrated_count)


def main() -> None:
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL, logging.INFO),
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    )

    logger.info("=" * 60)
    logger.info("Slack → Google Chat Migration")
    logger.info("=" * 60)

    state = _load_state()

    # --- Slack export ---
    slack = SlackExporter(config.SLACK_BOT_TOKEN)
    channels = slack.list_channels(filter_names=config.SLACK_CHANNELS)

    if not channels:
        logger.warning("No channels found to migrate. Check SLACK_CHANNELS in .env.")
        sys.exit(0)

    logger.info("Channels to migrate: %s", ", ".join(f"#{c.name}" for c in channels))

    # --- Google Chat import ---
    gchat = GoogleChatImporter(
        auth_mode=config.GOOGLE_AUTH_MODE,
        client_secret_path=config.GOOGLE_OAUTH_CLIENT_SECRET_PATH,
        service_account_key_path=config.GOOGLE_SERVICE_ACCOUNT_KEY_PATH,
        delegated_user_email=config.GOOGLE_DELEGATED_USER_EMAIL,
        rate_limit=config.GCHAT_RATE_LIMIT,
    )

    # Filter out already-migrated channels to avoid expensive Slack API calls.
    already_done = set(state.get("migrated_channels", {}).keys())
    pending = [ch for ch in channels if ch.name not in already_done]
    if already_done:
        skipped_names = [ch.name for ch in channels if ch.name in already_done]
        logger.info("⏭  Skipping %d already-migrated channel(s): %s",
                     len(skipped_names), ", ".join(f"#{n}" for n in skipped_names))
    logger.info("📋 %d channel(s) remaining to migrate.", len(pending))

    for channel in pending:
        try:
            full_channel = slack.export_channel(channel)
            _migrate_channel(full_channel, gchat, state)

            # Invite channel members to the Google Chat space.
            if config.INVITE_MEMBERS:
                space_name = state["migrated_channels"].get(channel.name, {}).get("space_name")
                if space_name:
                    invited_key = f"invited:{channel.name}"
                    skipped_key = f"skipped:{channel.name}"
                    already_invited: set[str] = set(state.get(invited_key, []))
                    already_skipped: set[str] = set(state.get(skipped_key, []))
                    emails = slack.get_channel_member_emails(channel.id)
                    new_emails = [e for e in emails
                                  if e not in already_invited and e not in already_skipped]
                    if new_emails:
                        logger.info("Inviting %d member(s) to space for #%s …",
                                    len(new_emails), channel.name)
                        added = 0
                        skipped = 0
                        failed = 0
                        for email in new_emails:
                            result = gchat.invite_member(space_name, email)
                            if result == "added":
                                logger.info("  ✓ Invited %s", email)
                                already_invited.add(email)
                                added += 1
                            elif result == "exists":
                                already_invited.add(email)
                            elif result == "not_found":
                                already_skipped.add(email)
                                skipped += 1
                            else:  # "failed" — don't checkpoint so it retries next run
                                failed += 1
                        state[invited_key] = list(already_invited)
                        state[skipped_key] = list(already_skipped)
                        _save_state(state)
                        logger.info("  Invite summary for #%s: %d added, %d not in workspace, %d failed",
                                    channel.name, added, skipped, failed)
                    else:
                        logger.info("All members already invited for #%s.", channel.name)
            # Archive the Slack channel after successful migration + invites.
            if config.ARCHIVE_AFTER_MIGRATE:
                space_name_for_archive = state["migrated_channels"].get(channel.name, {}).get("space_name")
                if space_name_for_archive:
                    _archive_slack_channel(channel.id, channel.name, state)

        except Exception:
            logger.exception("❌ Failed to migrate #%s", channel.name)
            if channel.name not in state["failed_channels"]:
                state["failed_channels"].append(channel.name)
            _save_state(state)

    # --- Summary ---
    logger.info("=" * 60)
    logger.info("Migration complete!")
    logger.info("  Migrated: %d channel(s)", len(state["migrated_channels"]))
    if state["failed_channels"]:
        logger.warning("  Failed:   %s", ", ".join(state["failed_channels"]))
        logger.warning("  Re-run the script to retry failed channels.")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
