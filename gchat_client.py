"""Google Chat import — creates Spaces and posts messages via the Chat API."""

import logging
import os
import time
from datetime import datetime
from pathlib import Path

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Scopes needed for user OAuth flow (no chat.bot for user creds)
OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.spaces.create",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.memberships",
]

# Scopes for service account with domain-wide delegation
SA_SCOPES = [
    "https://www.googleapis.com/auth/chat.bot",
    "https://www.googleapis.com/auth/chat.spaces",
    "https://www.googleapis.com/auth/chat.spaces.create",
    "https://www.googleapis.com/auth/chat.messages",
    "https://www.googleapis.com/auth/chat.messages.create",
    "https://www.googleapis.com/auth/chat.memberships",
]

TOKEN_PATH = Path("token.json")


def _ts_to_readable(ts: str) -> str:
    """Convert a Slack epoch timestamp to a human-readable datetime string."""
    try:
        epoch = float(ts)
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError, OSError):
        return ts


def _get_oauth_credentials(client_secret_path: str) -> Credentials:
    """Obtain user OAuth credentials, refreshing or prompting login as needed."""
    creds: Credentials | None = None

    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), OAUTH_SCOPES)

    if creds and creds.expired and creds.refresh_token:
        logger.info("Refreshing expired OAuth token …")
        creds.refresh(Request())
    elif not creds or not creds.valid:
        logger.info("Opening browser for Google Chat authorization …")
        flow = InstalledAppFlow.from_client_secrets_file(client_secret_path, OAUTH_SCOPES)
        creds = flow.run_local_server(port=0)

    # Persist for next run.
    TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")
    return creds


class GoogleChatImporter:
    """Imports messages into Google Chat Spaces."""

    def __init__(
        self,
        auth_mode: str = "oauth",
        client_secret_path: str = "",
        service_account_key_path: str = "",
        delegated_user_email: str = "",
        rate_limit: int = 5,
    ) -> None:
        self._rate_limit = rate_limit
        self._last_call_time: float = 0.0
        self._spaces_cache: list[dict] | None = None

        if auth_mode == "service_account":
            creds = service_account.Credentials.from_service_account_file(
                service_account_key_path,
                scopes=SA_SCOPES,
                subject=delegated_user_email,
            )
        else:
            creds = _get_oauth_credentials(client_secret_path)

        self._creds = creds
        self._client_secret_path = client_secret_path
        self._service = build("chat", "v1", credentials=creds)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _ensure_credentials(self) -> None:
        """Refresh OAuth credentials if they have expired."""
        if hasattr(self._creds, "expired") and self._creds.expired and self._creds.refresh_token:
            logger.info("Refreshing expired OAuth token …")
            self._creds.refresh(Request())
            TOKEN_PATH.write_text(self._creds.to_json(), encoding="utf-8")
            self._service = build("chat", "v1", credentials=self._creds)

    def find_or_create_space(self, display_name: str) -> str:
        """Find an existing space by display name, or create a new one.

        Returns the space `name` (e.g. "spaces/AAAA1234").
        """
        self._ensure_credentials()

        # Search through cached spaces first, then refresh if not found.
        if self._spaces_cache is None:
            self._spaces_cache = self._list_spaces()

        for space in self._spaces_cache:
            if space.get("displayName") == display_name:
                logger.info("Found existing space: %s", display_name)
                return space["name"]

        # Create a new named space.
        logger.info("Creating new Google Chat space: %s", display_name)
        body = {
            "displayName": display_name,
            "spaceType": "SPACE",
        }
        result = self._execute_with_retry(
            lambda: self._service.spaces().create(body=body).execute()
        )
        # Add to cache so subsequent calls don't re-list.
        self._spaces_cache.append(result)
        logger.info("Created space: %s → %s", display_name, result["name"])
        return result["name"]

    def invite_member(self, space_name: str, email: str) -> str:
        """Add a user to a Google Chat space by email.

        Returns:
            "added"  — successfully invited
            "exists" — already a member (no action needed)
            "not_found" — user does not exist in Google Workspace
            "failed" — other error
        """
        self._throttle()
        self._ensure_credentials()
        body = {
            "member": {
                "name": f"users/{email}",
                "type": "HUMAN",
            },
        }
        try:
            # Single attempt — client errors (4xx) are not retryable.
            self._service.spaces().members().create(
                parent=space_name, body=body
            ).execute()
            return "added"
        except HttpError as exc:
            status = exc.resp.status
            if status == 409:  # Already a member
                logger.debug("  Already member: %s", email)
                return "exists"
            if status in (403, 404):
                logger.warning(
                    "  ⚠ User not in Google Workspace, skipping: %s (HTTP %d)",
                    email, status,
                )
                return "not_found"
            if status == 429 or status >= 500:
                # Retry once for rate-limit / server errors.
                retry_after = int(exc.resp.get("retry-after", 10))
                logger.warning("  Rate-limited inviting %s — waiting %ds …", email, retry_after)
                time.sleep(retry_after)
                try:
                    self._service.spaces().members().create(
                        parent=space_name, body=body
                    ).execute()
                    return "added"
                except HttpError:
                    pass
            logger.error("  ❌ Failed to invite %s: HTTP %d — %s", email, status, exc)
            return "failed"

    def post_message(self, space_name: str, text: str) -> str:
        """Post a top-level message to a space. Returns the thread `name`."""
        self._throttle()
        self._ensure_credentials()
        body = {"text": text}
        result = self._execute_with_retry(
            lambda: self._service.spaces().messages().create(
                parent=space_name, body=body
            ).execute()
        )
        # Return the thread name so replies can target it correctly.
        return result.get("thread", {}).get("name", result["name"])

    def post_thread_reply(self, space_name: str, thread_name: str, text: str) -> str:
        """Post a reply in an existing thread. Returns the message `name`."""
        self._throttle()
        self._ensure_credentials()
        body = {
            "text": text,
            "thread": {"name": thread_name},
        }
        result = self._execute_with_retry(
            lambda: self._service.spaces().messages().create(
                parent=space_name,
                body=body,
                messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
            ).execute()
        )
        return result["name"]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_spaces(self) -> list[dict]:
        """Return all spaces visible to the authenticated user."""
        spaces: list[dict] = []
        page_token: str | None = None
        while True:
            pt = page_token or ""
            resp = self._execute_with_retry(
                lambda: self._service.spaces().list(
                    pageSize=100,
                    pageToken=pt,
                ).execute()
            )
            spaces.extend(resp.get("spaces", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        return spaces

    def _throttle(self) -> None:
        """Enforce rate limiting between API calls."""
        if self._rate_limit <= 0:
            return
        interval = 1.0 / self._rate_limit
        elapsed = time.time() - self._last_call_time
        if elapsed < interval:
            time.sleep(interval - elapsed)
        self._last_call_time = time.time()

    @staticmethod
    def _execute_with_retry(api_call, max_retries: int = 10):
        """Execute a Google API call with back-off on 429/5xx.

        `api_call` must be a callable (lambda) that builds AND executes
        the request.  A fresh HttpRequest is created on each retry so
        that the request object is never reused after failure.

        Respects the Retry-After header from 429 responses.
        Falls back to exponential backoff capped at 120 s.
        """
        for attempt in range(max_retries):
            try:
                return api_call()
            except HttpError as exc:
                status = exc.resp.status
                if status == 429 or status >= 500:
                    retry_after = exc.resp.get("retry-after")
                    if retry_after:
                        wait = min(int(retry_after), 120)
                    else:
                        wait = min(2 ** attempt, 120)
                    logger.warning(
                        "Google Chat API error %d — retrying in %ds (attempt %d/%d) …",
                        status, wait, attempt + 1, max_retries,
                    )
                    time.sleep(wait)
                    continue
                raise
        raise RuntimeError(f"Google Chat API call failed after {max_retries} retries.")


def format_slack_message(
    user_name: str,
    text: str,
    ts: str,
    files: tuple = (),
    reactions: tuple = (),
    include_file_links: bool = True,
) -> str:
    """Format a Slack message into a plain-text representation for Google Chat."""
    timestamp = _ts_to_readable(ts)
    lines = [f"*{user_name}*  _{timestamp}_", ""]

    if text:
        lines.append(text)

    if include_file_links and files:
        lines.append("")
        for f in files:
            lines.append(f"📎 [{f.name}]({f.url})")

    if reactions:
        lines.append("")
        lines.append("Reactions: " + " ".join(reactions))

    return "\n".join(lines)
