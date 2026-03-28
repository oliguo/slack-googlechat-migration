"""Download all Slack file attachments and build a CSV manifest.

Scans every channel the bot can see, fetches messages + threads,
extracts file attachments, and downloads them into a local folder
organised by channel name.  A CSV manifest maps each file back to
its originating message.

Features:
  - Concurrent downloads (configurable worker count)
  - Automatic retry with exponential backoff on network / API errors
  - Resumable — skips files that already exist on disk (same name + size)
  - Per-channel progress tracking via a JSON state file

Usage:
    python download_files.py                       # all channels
    python download_files.py --channels ch1,ch2    # specific channels
    python download_files.py --workers 8           # 8 parallel downloads
"""

import csv
import json
import logging
import os
import re
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import requests
from dotenv import load_dotenv
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)
logger = logging.getLogger("download_files")

SLACK_BOT_TOKEN: str = os.getenv("SLACK_BOT_TOKEN", "").strip()
if not SLACK_BOT_TOKEN:
    sys.exit("ERROR: SLACK_BOT_TOKEN is not set in .env")

DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "downloads"))
STATE_FILE = Path("download_state.json")
MAX_RETRIES = int(os.getenv("DOWNLOAD_MAX_RETRIES", "5"))
BACKOFF_BASE = 2  # seconds — exponential: 2, 4, 8, 16, 32
DEFAULT_WORKERS = int(os.getenv("DOWNLOAD_WORKERS", "4"))
DOWNLOAD_CHANNELS_ENV: str = os.getenv("DOWNLOAD_CHANNELS", "").strip()

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileRecord:
    channel_name: str
    message_ts: str
    message_text: str
    thread_ts: str | None
    user_name: str
    file_id: str
    file_name: str
    file_url: str
    file_size: int
    mimetype: str


# ---------------------------------------------------------------------------
# State persistence (resumable)
# ---------------------------------------------------------------------------


def _record_to_dict(r: FileRecord) -> dict:
    return {
        "channel_name": r.channel_name,
        "message_ts": r.message_ts,
        "thread_ts": r.thread_ts,
        "user_name": r.user_name,
        "file_id": r.file_id,
        "file_name": r.file_name,
        "file_url": r.file_url,
        "file_size": r.file_size,
        "mimetype": r.mimetype,
    }


def _record_from_dict(d: dict) -> FileRecord:
    return FileRecord(
        channel_name=d["channel_name"],
        message_ts=d["message_ts"],
        message_text=d.get("message_text", ""),
        thread_ts=d.get("thread_ts"),
        user_name=d["user_name"],
        file_id=d["file_id"],
        file_name=d["file_name"],
        file_url=d["file_url"],
        file_size=d.get("file_size", 0),
        mimetype=d.get("mimetype", ""),
    )


def _load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"completed_channels": [], "downloaded_files": {}, "scanned_channels": {}}


def _save_state(state: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=STATE_FILE.parent, suffix=".tmp", prefix=".dl_state_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, STATE_FILE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Slack helpers
# ---------------------------------------------------------------------------

_user_cache: dict[str, str] = {}


def _api_call(method, **kwargs):
    """Call a Slack API method with retry on rate-limits and network errors."""
    max_network_retries = 5
    for attempt in range(1, max_network_retries + 1):
        try:
            return method(**kwargs)
        except SlackApiError as exc:
            if exc.response.status_code == 429:
                retry_after = int(exc.response.headers.get("Retry-After", 5))
                logger.warning("Rate-limited — waiting %ds …", retry_after)
                time.sleep(retry_after)
                continue
            raise
        except Exception as exc:
            # Transient network errors: IncompleteRead, ConnectionError, etc.
            if attempt < max_network_retries:
                wait = BACKOFF_BASE ** attempt
                logger.warning(
                    "Network error (attempt %d/%d): %s — retrying in %ds …",
                    attempt, max_network_retries, exc, wait,
                )
                time.sleep(wait)
                continue
            raise


def _resolve_user(client: WebClient, user_id: str) -> str:
    if not user_id:
        return "unknown"
    if user_id in _user_cache:
        return _user_cache[user_id]
    try:
        resp = _api_call(client.users_info, user=user_id)
        name = resp["user"].get("real_name") or resp["user"].get("name", user_id)
    except SlackApiError:
        name = user_id
    _user_cache[user_id] = name
    return name


def _fetch_channels(client: WebClient, filter_names: set[str] | None) -> list[dict]:
    channels: list[dict] = []
    for channel_type in ("public_channel", "private_channel"):
        cursor = None
        while True:
            resp = _api_call(
                client.conversations_list,
                types=channel_type,
                limit=200,
                cursor=cursor,
            )
            for ch in resp["channels"]:
                name = ch.get("name", "")
                if filter_names and name not in filter_names:
                    continue
                channels.append(ch)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    return channels


def _extract_files_from_raw(
    raw: dict,
    channel_name: str,
    client: WebClient,
    thread_ts: str | None = None,
) -> list[FileRecord]:
    """Extract FileRecord entries from a raw Slack message dict."""
    records: list[FileRecord] = []
    user_name = _resolve_user(client, raw.get("user", ""))
    text = raw.get("text", "")
    ts = raw.get("ts", "")

    for f in raw.get("files", []):
        # Skip tombstoned / deleted files
        if f.get("mode") == "tombstone":
            continue
        # Skip external files (Google Drive, Dropbox, etc.) — only Slack-hosted uploads
        if f.get("mode") == "external":
            continue
        url = f.get("url_private_download") or f.get("url_private") or f.get("permalink", "")
        if not url:
            continue
        records.append(FileRecord(
            channel_name=channel_name,
            message_ts=ts,
            message_text=text[:500],  # Truncate long texts for CSV
            thread_ts=thread_ts,
            user_name=user_name,
            file_id=f.get("id", ""),
            file_name=f.get("name", "file"),
            file_url=url,
            file_size=f.get("size", 0),
            mimetype=f.get("mimetype", ""),
        ))
    return records


def _scan_channel(client: WebClient, channel: dict) -> list[FileRecord]:
    """Scan all messages + threads in a channel for file attachments."""
    ch_id = channel["id"]
    ch_name = channel["name"]
    all_files: list[FileRecord] = []

    # Fetch top-level messages
    messages: list[dict] = []
    cursor = None
    while True:
        resp = _api_call(
            client.conversations_history,
            channel=ch_id,
            limit=200,
            cursor=cursor,
        )
        messages.extend(resp.get("messages", []))
        cursor = resp.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break

    logger.info("  → %d messages in #%s", len(messages), ch_name)

    for raw in messages:
        # Files in the top-level message
        all_files.extend(_extract_files_from_raw(raw, ch_name, client))

        # If this message has thread replies, fetch them
        if raw.get("reply_count", 0) > 0:
            thread_ts = raw["ts"]
            reply_cursor = None
            while True:
                resp = _api_call(
                    client.conversations_replies,
                    channel=ch_id,
                    ts=thread_ts,
                    limit=200,
                    cursor=reply_cursor,
                )
                for reply in resp.get("messages", []):
                    if reply["ts"] == thread_ts:
                        continue  # Parent already processed
                    all_files.extend(
                        _extract_files_from_raw(reply, ch_name, client, thread_ts=thread_ts)
                    )
                reply_cursor = resp.get("response_metadata", {}).get("next_cursor")
                if not reply_cursor:
                    break

    return all_files


# ---------------------------------------------------------------------------
# File download with retry
# ---------------------------------------------------------------------------

_SANITIZE_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    """Remove characters that are invalid in file paths."""
    return _SANITIZE_RE.sub("_", name).strip(". ")


def _download_one(
    record: FileRecord,
    token: str,
    state: dict,
    state_lock,
) -> tuple[bool, FileRecord, str]:
    """Download a single file. Returns (success, record, local_path_or_error)."""
    channel_dir = DOWNLOAD_DIR / _sanitize_filename(record.channel_name)
    channel_dir.mkdir(parents=True, exist_ok=True)

    # Deduplicate filenames within a channel by prefixing with file_id
    safe_name = _sanitize_filename(record.file_name)
    local_name = f"{record.file_id}_{safe_name}" if record.file_id else safe_name
    local_path = channel_dir / local_name

    # Check if already downloaded (same name exists with matching size)
    state_key = f"{record.channel_name}/{record.file_id}"
    if state_key in state.get("downloaded_files", {}):
        stored = state["downloaded_files"][state_key]
        if local_path.exists() and local_path.stat().st_size == stored.get("size", -1):
            return True, record, str(local_path)

    # Download with retry + exponential backoff
    headers = {"Authorization": f"Bearer {token}"}
    last_error = ""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(record.file_url, headers=headers, timeout=120, stream=True)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 5))
                logger.warning("  Rate-limited downloading %s — waiting %ds", local_name, retry_after)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()

            # Write to temp file then rename for atomicity
            fd, tmp = tempfile.mkstemp(dir=channel_dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        f.write(chunk)
                os.replace(tmp, local_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

            actual_size = local_path.stat().st_size

            # Record in state
            with state_lock:
                state.setdefault("downloaded_files", {})[state_key] = {
                    "size": actual_size,
                    "file_id": record.file_id,
                }
            return True, record, str(local_path)

        except requests.RequestException as exc:
            last_error = str(exc)
            wait = BACKOFF_BASE ** attempt
            if attempt < MAX_RETRIES:
                logger.warning(
                    "  Retry %d/%d for %s — %s (waiting %ds)",
                    attempt, MAX_RETRIES, local_name, last_error, wait,
                )
                time.sleep(wait)

    logger.error("  ❌ Failed to download %s after %d attempts: %s", local_name, MAX_RETRIES, last_error)
    return False, record, last_error


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _format_size(size_bytes: int) -> str:
    """Return a human-readable file size string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size_bytes) < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


# ---------------------------------------------------------------------------
# CSV writing
# ---------------------------------------------------------------------------


def _write_csv(records: list[tuple[FileRecord, str]], csv_path: Path) -> None:
    """Write the manifest CSV."""
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "channel", "message_ts", "thread_ts", "user", "message_text",
            "file_id", "file_name", "file_url", "mimetype", "local_path",
        ])
        for rec, local in records:
            writer.writerow([
                rec.channel_name,
                rec.message_ts,
                rec.thread_ts or "",
                rec.user_name,
                rec.message_text,
                rec.file_id,
                rec.file_name,
                rec.file_url,
                rec.mimetype,
                local,
            ])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    import argparse
    import threading

    parser = argparse.ArgumentParser(description="Download all Slack file attachments.")
    parser.add_argument("--channels", type=str, default="",
                        help="Comma-separated channel names. Overrides DOWNLOAD_CHANNELS env var.")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                        help=f"Parallel download workers (default: {DEFAULT_WORKERS}, env: DOWNLOAD_WORKERS).")
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip confirmation prompt and start downloading immediately.")
    parser.add_argument("--scan-only", action="store_true",
                        help="Only scan channels and show summary — do not download.")
    parser.add_argument("--rescan", action="store_true",
                        help="Force re-scan all channels (ignore cached scan results).")
    args = parser.parse_args()

    # CLI --channels > DOWNLOAD_CHANNELS env > all channels
    channels_raw = args.channels or DOWNLOAD_CHANNELS_ENV
    filter_names: set[str] | None = None
    if channels_raw:
        filter_names = {n.strip().lstrip("#") for n in channels_raw.split(",") if n.strip()}

    client = WebClient(token=SLACK_BOT_TOKEN)
    state = _load_state()
    state_lock = threading.Lock()

    logger.info("=" * 60)
    logger.info("Slack File Downloader")
    logger.info("=" * 60)

    # --- Fetch channels ---
    logger.info("Fetching channel list …")
    channels = _fetch_channels(client, filter_names)
    if not channels:
        logger.warning("No channels found.")
        return

    completed_set = set(state.get("completed_channels", []))
    pending = [ch for ch in channels if ch["name"] not in completed_set]
    if completed_set:
        logger.info("⏭  Skipping %d already-completed channel(s).", len(completed_set))
    logger.info("📋 %d channel(s) to scan for files.", len(pending))

    # =================================================================
    # Phase 1: Scan all channels and collect file records
    #          (uses cached scan results from state when available)
    # =================================================================
    cached_scans: dict[str, list[dict]] = state.get("scanned_channels", {})
    channel_records: dict[str, list[FileRecord]] = {}  # ch_name → records
    empty_channels: list[str] = []  # channels with 0 files
    restored_count = 0

    for ch_idx, channel in enumerate(pending, start=1):
        ch_name = channel["name"]

        # Restore from cache if available and --rescan not set
        if not args.rescan and ch_name in cached_scans:
            cached = cached_scans[ch_name]
            if cached:  # non-empty list → has files
                file_records = [_record_from_dict(d) for d in cached]
                channel_records[ch_name] = file_records
                restored_count += 1
            # else: cached as empty → already marked completed previously
            continue

        logger.info("[%d/%d] Scanning #%s …", ch_idx, len(pending), ch_name)

        try:
            file_records = _scan_channel(client, channel)
        except Exception:
            logger.exception("❌ Failed to scan #%s — skipping.", ch_name)
            continue

        # Cache scan results to state (even empty ones)
        state.setdefault("scanned_channels", {})[ch_name] = [
            _record_to_dict(r) for r in file_records
        ]
        _save_state(state)

        if not file_records:
            logger.info("  No files in #%s.", ch_name)
            empty_channels.append(ch_name)
            continue

        channel_records[ch_name] = file_records
        ch_size = sum(r.file_size for r in file_records)
        logger.info("  📎 %d file(s), %s", len(file_records), _format_size(ch_size))

    if restored_count:
        logger.info("⚡ Restored scan results for %d channel(s) from cache.", restored_count)

    # Mark empty channels as completed now
    for ch_name in empty_channels:
        state.setdefault("completed_channels", []).append(ch_name)
    if empty_channels:
        _save_state(state)

    # =================================================================
    # Phase 2: Show summary and ask for confirmation
    # =================================================================
    total_files = sum(len(recs) for recs in channel_records.values())
    total_size = sum(r.file_size for recs in channel_records.values() for r in recs)

    if total_files == 0:
        logger.info("No files to download across all channels.")
        return

    # Already-downloaded files that will be skipped
    already_downloaded = state.get("downloaded_files", {})
    skip_count = 0
    skip_size = 0
    for recs in channel_records.values():
        for r in recs:
            state_key = f"{r.channel_name}/{r.file_id}"
            if state_key in already_downloaded:
                skip_count += 1
                skip_size += r.file_size

    new_files = total_files - skip_count
    new_size = total_size - skip_size

    print("\n" + "=" * 60)
    print("  📊  SCAN SUMMARY")
    print("=" * 60)
    print(f"  Channels with files:  {len(channel_records)}")
    print(f"  Total files found:    {total_files:,}  ({_format_size(total_size)})")
    if skip_count:
        print(f"  Already downloaded:   {skip_count:,}  ({_format_size(skip_size)})")
        print(f"  New to download:      {new_files:,}  ({_format_size(new_size)})")
    print()

    # Per-channel breakdown (sorted by size descending)
    sorted_channels = sorted(
        channel_records.items(),
        key=lambda kv: sum(r.file_size for r in kv[1]),
        reverse=True,
    )
    print(f"  {'Channel':<35} {'Files':>7}  {'Size':>10}")
    print(f"  {'-' * 35} {'-' * 7}  {'-' * 10}")
    for ch_name, recs in sorted_channels:
        ch_size = sum(r.file_size for r in recs)
        print(f"  #{ch_name:<34} {len(recs):>7,}  {_format_size(ch_size):>10}")
    print("=" * 60)

    if args.scan_only:
        logger.info("Scan-only mode — exiting without downloading.")
        return

    if not args.yes:
        try:
            answer = input("\n  Proceed with download? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            logger.info("Cancelled.")
            return
        if answer not in ("y", "yes"):
            logger.info("Cancelled by user.")
            return

    # =================================================================
    # Phase 3: Download
    # =================================================================
    all_records: list[FileRecord] = []
    total_csv_entries = 0

    # Refresh completed set (may have grown during Phase 1 scan)
    completed_set = set(state.get("completed_channels", []))

    for ch_idx, (ch_name, file_records) in enumerate(sorted_channels, start=1):
        if ch_name in completed_set:
            logger.info("[%d/%d] ⏭  Skipping already-completed #%s", ch_idx, len(sorted_channels), ch_name)
            continue
        logger.info("")
        logger.info("[%d/%d] Downloading %d file(s) from #%s …",
                    ch_idx, len(sorted_channels), len(file_records), ch_name)
        all_records.extend(file_records)

        # Parallel download
        succeeded = 0
        failed = 0
        ch_csv_rows: list[tuple[FileRecord, str]] = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(_download_one, rec, SLACK_BOT_TOKEN, state, state_lock): rec
                for rec in file_records
            }
            for future in as_completed(futures):
                ok, rec, result_path = future.result()
                if ok:
                    ch_csv_rows.append((rec, result_path))
                    succeeded += 1
                else:
                    failed += 1

        logger.info("  ✅ %d downloaded, ❌ %d failed in #%s", succeeded, failed, ch_name)

        # Write per-channel CSV manifest
        if ch_csv_rows:
            ch_csv_path = DOWNLOAD_DIR / _sanitize_filename(ch_name) / "file_manifest.csv"
            _write_csv(ch_csv_rows, ch_csv_path)
            total_csv_entries += len(ch_csv_rows)
            logger.info("  📄 CSV manifest: %s (%d entries)", ch_csv_path, len(ch_csv_rows))

        # Mark channel complete, remove from scan cache, and flush state
        if failed == 0:
            state.setdefault("completed_channels", []).append(ch_name)
            state.get("scanned_channels", {}).pop(ch_name, None)
        _save_state(state)

    # --- Summary ---
    logger.info("")
    logger.info("=" * 60)
    total_downloaded = len(state.get("downloaded_files", {}))
    logger.info("Done! Total files downloaded: %d", total_downloaded)
    if total_csv_entries:
        logger.info("CSV manifests written per channel (%d total entries)", total_csv_entries)
    logger.info("Download directory: %s", DOWNLOAD_DIR.resolve())
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
