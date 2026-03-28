"""Slack data export — fetches channels, messages, and threads from Slack."""

import logging
import time
from dataclasses import dataclass, field

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SlackFile:
    name: str
    url: str
    mimetype: str


@dataclass(frozen=True)
class SlackMessage:
    ts: str
    user_id: str
    user_name: str
    text: str
    thread_ts: str | None = None
    reply_count: int = 0
    files: tuple[SlackFile, ...] = ()
    reactions: tuple[str, ...] = ()


@dataclass(frozen=True)
class SlackThread:
    parent: SlackMessage
    replies: tuple[SlackMessage, ...] = ()


@dataclass(frozen=True)
class SlackChannel:
    id: str
    name: str
    topic: str
    purpose: str
    is_private: bool
    messages: tuple[SlackMessage, ...] = ()
    threads: tuple[SlackThread, ...] = ()


class SlackExporter:
    """Exports data from Slack using the Slack Web API."""

    def __init__(self, token: str) -> None:
        self._client = WebClient(token=token)
        self._user_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def list_channels(self, filter_names: list[str] | None = None) -> list[SlackChannel]:
        """Return a list of channels (public + private) the bot can see."""
        channels: list[SlackChannel] = []

        for channel_type in ("public_channel", "private_channel"):
            cursor = None
            while True:
                resp = self._api_call(
                    self._client.conversations_list,
                    types=channel_type,
                    limit=200,
                    cursor=cursor,
                )
                for ch in resp["channels"]:
                    name = ch.get("name", "")
                    if filter_names and name not in filter_names:
                        continue
                    channels.append(SlackChannel(
                        id=ch["id"],
                        name=name,
                        topic=ch.get("topic", {}).get("value", ""),
                        purpose=ch.get("purpose", {}).get("value", ""),
                        is_private=ch.get("is_private", False),
                    ))
                cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break

        logger.info("Found %d channel(s) to migrate.", len(channels))
        return channels

    def get_channel_member_emails(self, channel_id: str) -> list[str]:
        """Return email addresses for all members of a Slack channel."""
        member_ids: list[str] = []
        cursor = None
        while True:
            resp = self._api_call(
                self._client.conversations_members,
                channel=channel_id,
                limit=200,
                cursor=cursor,
            )
            member_ids.extend(resp.get("members", []))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        emails: list[str] = []
        for uid in member_ids:
            try:
                resp = self._api_call(self._client.users_info, user=uid)
                user = resp.get("user", {})
                # Skip bots and Slackbot
                if user.get("is_bot") or user.get("id") == "USLACKBOT":
                    continue
                email = user.get("profile", {}).get("email")
                if email:
                    emails.append(email)
            except SlackApiError:
                logger.debug("Could not fetch user info for %s", uid)
        logger.info("  → %d member email(s) found for channel %s", len(emails), channel_id)
        return emails

    def export_channel(self, channel: SlackChannel) -> SlackChannel:
        """Fetch all messages and threads for a channel. Returns a new channel object."""
        logger.info("Exporting channel #%s …", channel.name)
        messages = self._fetch_all_messages(channel.id)
        logger.info("  → %d top-level messages", len(messages))

        threads: list[SlackThread] = []
        for msg in messages:
            if msg.reply_count > 0:
                replies = self._fetch_thread_replies(channel.id, msg.ts)
                threads.append(SlackThread(parent=msg, replies=tuple(replies)))
                logger.debug("  → Thread %s: %d replies", msg.ts, len(replies))

        logger.info("  → %d thread(s) with replies", len(threads))

        return SlackChannel(
            id=channel.id,
            name=channel.name,
            topic=channel.topic,
            purpose=channel.purpose,
            is_private=channel.is_private,
            messages=tuple(messages),
            threads=tuple(threads),
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_user(self, user_id: str) -> str:
        if not user_id:
            return "unknown"
        if user_id in self._user_cache:
            return self._user_cache[user_id]
        try:
            resp = self._api_call(self._client.users_info, user=user_id)
            name = resp["user"].get("real_name") or resp["user"].get("name", user_id)
        except SlackApiError:
            name = user_id
        self._user_cache[user_id] = name
        return name

    def _parse_message(self, raw: dict) -> SlackMessage:
        user_id = raw.get("user", "")
        files = tuple(
            SlackFile(
                name=f.get("name", "file"),
                url=f.get("url_private", f.get("permalink", "")),
                mimetype=f.get("mimetype", ""),
            )
            for f in raw.get("files", [])
        )
        reactions = tuple(
            f":{r['name']}:" for r in raw.get("reactions", [])
        )
        return SlackMessage(
            ts=raw["ts"],
            user_id=user_id,
            user_name=self._resolve_user(user_id),
            text=raw.get("text", ""),
            thread_ts=raw.get("thread_ts"),
            reply_count=raw.get("reply_count", 0),
            files=files,
            reactions=reactions,
        )

    def _fetch_all_messages(self, channel_id: str) -> list[SlackMessage]:
        messages: list[SlackMessage] = []
        cursor = None
        while True:
            resp = self._api_call(
                self._client.conversations_history,
                channel=channel_id,
                limit=200,
                cursor=cursor,
            )
            for raw in resp.get("messages", []):
                # Skip thread replies in the main timeline — they are fetched separately.
                if raw.get("thread_ts") and raw.get("thread_ts") != raw.get("ts"):
                    continue
                messages.append(self._parse_message(raw))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        # Slack returns newest-first; reverse for chronological order.
        messages.reverse()
        return messages

    def _fetch_thread_replies(self, channel_id: str, thread_ts: str) -> list[SlackMessage]:
        replies: list[SlackMessage] = []
        cursor = None
        while True:
            resp = self._api_call(
                self._client.conversations_replies,
                channel=channel_id,
                ts=thread_ts,
                limit=200,
                cursor=cursor,
            )
            for raw in resp.get("messages", []):
                # First message is the parent — skip it.
                if raw["ts"] == thread_ts:
                    continue
                replies.append(self._parse_message(raw))
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
        return replies

    @staticmethod
    def _api_call(method, **kwargs):
        """Call a Slack API method with automatic rate-limit retry."""
        while True:
            try:
                return method(**kwargs)
            except SlackApiError as exc:
                if exc.response.status_code == 429:
                    retry_after = int(exc.response.headers.get("Retry-After", 5))
                    logger.warning("Slack rate-limited — waiting %ds …", retry_after)
                    time.sleep(retry_after)
                    continue
                raise
