# Slack → Google Chat Migration Toolkit

A complete toolkit to migrate Slack channels — messages, threads, reactions, file attachments, and member lists — into Google Chat Spaces. Designed for large workspaces with 100+ channels, with full resume support, rate limiting, and overnight-run stability.

---

## Table of Contents

1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Quick Start](#quick-start)
4. [Set Up the Slack Bot](#set-up-the-slack-bot)
5. [Set Up Google Cloud & Chat API](#set-up-google-cloud--chat-api)
6. [Configure the Tool](#configure-the-tool)
7. [Migration Workflow](#migration-workflow)
8. [Scripts Reference](#scripts-reference)
9. [Environment Variables](#environment-variables)
10. [State Files & Resume](#state-files--resume)
11. [Verify & Troubleshoot](#verify--troubleshoot)
12. [Important Notes & Limitations](#important-notes--limitations)
13. [Security Notes](#security-notes)
14. [Project Structure](#project-structure)

---

## Overview

This toolkit provides a set of Python scripts that work together to:

| Step | Script | What It Does |
|------|--------|-------------|
| 1. Prepare | `prepare_channels.py` | Unarchive channels; invite bot to all channels |
| 2. (Optional) Convert | `convert_private_to_public.py` | Convert private channels → public for full Slack export |
| 3. Download files | `download_files.py` | Download all Slack-hosted file attachments to local disk |
| 4. Migrate | `migrate.py` | Export Slack messages → create Google Chat Spaces → import messages |
| 5. (Optional) Re-invite | `reinvite_skipped.py` | Retry inviting users who were not found in Google Workspace |
| 6. (Optional) Archive | `archive_migrated.py` | Archive the source Slack channels after migration |
| 7. (Optional) Check access | `enable_external_access.py` | Report which spaces allow external users |

All scripts are **resumable** — if interrupted, re-run the same command and it picks up where it left off.

---

## Prerequisites

- **Python 3.10+** (tested on 3.12)
- **macOS / Linux / Windows** (WSL recommended on Windows)
- A **Slack workspace** where you are an admin
- A **Google Workspace** account with Google Chat enabled (free Gmail accounts do **not** have the Chat API)
- Access to the **Google Cloud Console** ([console.cloud.google.com](https://console.cloud.google.com))

---

## Quick Start

```bash
# 1. Clone or download this folder
cd slack-googlechat-migration

# 2. Create and activate virtual environment
python3 -m venv venv
source venv/bin/activate        # macOS/Linux
# venv\Scripts\activate         # Windows

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure
cp .env.example .env
# Edit .env with your tokens and settings (see "Configure the Tool" below)

# 5. Place OAuth credentials
# Download client_secret.json from Google Cloud Console → save here
cp ~/Downloads/client_secret.json ./client_secret.json

# 6. Prepare channels (unarchive + invite bot)
python prepare_channels.py

# 7. Download file attachments (optional but recommended)
python download_files.py

# 8. Run migration
python migrate.py

# 9. Archive old Slack channels (optional)
python archive_migrated.py --apply
```

---

## Set Up the Slack Bot

### Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps)
2. Click **"Create New App"** → **"From scratch"**
3. Name it (e.g. `Migration Bot`), select your workspace, click **Create App**

### Add Bot Token Scopes

Go to **OAuth & Permissions** → **Bot Token Scopes** and add:

| Scope | Purpose |
|-------|---------|
| `channels:history` | Read messages from public channels |
| `channels:read` | List public channels |
| `groups:history` | Read messages from private channels |
| `groups:read` | List private channels |
| `users:read` | Resolve user IDs to real names |
| `reactions:read` | Read emoji reactions on messages |
| `files:read` | Read file attachment metadata |

Click **"Install to Workspace"** and copy the **Bot User OAuth Token** (`xoxb-…`).

### Add User Token Scopes (for admin operations)

Some scripts (`prepare_channels.py`, `archive_migrated.py`, `convert_private_to_public.py`) need a **User Token** (`xoxp-…`) with additional scopes:

| Scope | Purpose | Used By |
|-------|---------|---------|
| `channels:manage` | Archive/unarchive channels | `archive_migrated.py`, `prepare_channels.py` |
| `groups:write` | Manage private channels | `archive_migrated.py`, `prepare_channels.py` |
| `channels:join` | Join public channels | `prepare_channels.py` |
| `admin.conversations:write` | Convert private → public | `convert_private_to_public.py` (Business+ plan only) |

> **How to get the User Token:** In your Slack App settings, go to **OAuth & Permissions** → add the scopes under **User Token Scopes** → reinstall the app → copy the **User OAuth Token** (`xoxp-…`).

### Invite the Bot to Channels

The bot can only read channels it belongs to.

- **Public channels**: Bot can join automatically, or type `/invite @Migration Bot` in each channel
- **Private channels**: You **must** manually invite the bot
- **Or**: Use `prepare_channels.py` to batch-invite the bot to all channels (recommended)

---

## Set Up Google Cloud & Chat API

### Create a Google Cloud Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a **New Project** (e.g. `slack-gchat-migration`)
3. Select the project

### Enable the Google Chat API

1. Go to **APIs & Services → Library**
2. Search for **"Google Chat API"** → click **Enable**

### Create OAuth 2.0 Desktop App Credentials (Recommended)

> **Why OAuth?** Many organizations block service account key creation via org policy. OAuth Desktop App credentials work everywhere and are the recommended approach.

1. Go to **APIs & Services → Credentials**
2. Click **"+ CREATE CREDENTIALS"** → **"OAuth client ID"**
3. If prompted to configure the **OAuth consent screen**:
   - User Type: **Internal** (for Google Workspace)
   - App name: `Slack Migration Tool`
   - User support email + developer contact: your email
   - Click **Save and Continue** through remaining steps
4. Back on Credentials → **"+ CREATE CREDENTIALS"** → **"OAuth client ID"**
5. Application type: **Desktop app** → Name: `Slack Migration CLI`
6. Click **Create** → **Download JSON**
7. Save as `client_secret.json` in this project folder

### Configure the Google Chat App

1. In Google Cloud Console → **APIs & Services → Google Chat API → Configuration** tab
2. Fill in:
   - **App name**: `Migration Bot`
   - **Description**: `Slack migration bot`
   - **Functionality**: Check both boxes (1:1 messages + join spaces)
   - **Connection settings**: App URL → `https://example.com` (placeholder)
   - **Visibility**: "Specific people" → add your user email
3. Click **Save**

> **First run:** A browser window opens for Google sign-in. After authorizing, a `token.json` is cached locally — you won't need to re-authorize unless the token is revoked.

### (Alternative) Service Account Method

<details>
<summary>Click to expand — only if your org allows service account key creation</summary>

1. Go to **Credentials** → **"+ CREATE CREDENTIALS"** → **"Service account"**
2. Name: `chat-migration-bot` → **Create and Continue** → skip role → **Done**
3. Click the service account → **Keys** tab → **Add Key → Create new key → JSON**
4. Save as `service_account.json` in this project folder
5. Enable **Domain-wide Delegation** on the service account
6. Note the **Client ID**
7. In [Google Workspace Admin](https://admin.google.com) → **Security → API controls → Domain Wide Delegation** → Add new:
   - Client ID: paste from above
   - Scopes:
     ```
     https://www.googleapis.com/auth/chat.bot,https://www.googleapis.com/auth/chat.spaces,https://www.googleapis.com/auth/chat.spaces.create,https://www.googleapis.com/auth/chat.messages,https://www.googleapis.com/auth/chat.messages.create,https://www.googleapis.com/auth/chat.memberships
     ```

Set `GOOGLE_AUTH_MODE=service_account` in `.env`.

</details>

---

## Configure the Tool

### Create the `.env` File

```bash
cp .env.example .env
```

### Edit `.env` with Your Values

```ini
# ─── Slack ───────────────────────────────────────────────
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_USER_TOKEN=xoxp-your-user-token          # needed for prepare/archive/convert scripts
SLACK_BOT_USER_ID=U0123456789                   # bot's user ID (for prepare_channels.py)

# ─── Channels ───────────────────────────────────────────
# Comma-separated channel names, or leave empty for ALL
SLACK_CHANNELS=

# ─── Google Chat ─────────────────────────────────────────
GOOGLE_AUTH_MODE=oauth
GOOGLE_OAUTH_CLIENT_SECRET_PATH=./client_secret.json

# ─── Migration Options ──────────────────────────────────
SPACE_NAME_PREFIX=[Migrated]                    # prefix for new Google Chat spaces
GCHAT_RATE_LIMIT=2                              # max messages/sec to Google Chat
INCLUDE_FILE_LINKS=true                         # include Slack file URLs in messages
INVITE_MEMBERS=true                             # auto-invite channel members to spaces
ARCHIVE_AFTER_MIGRATE=false                     # archive Slack channels after migration
LOG_LEVEL=INFO                                  # DEBUG, INFO, WARNING, ERROR

# ─── File Downloads (download_files.py) ──────────────────
# DOWNLOAD_DIR=downloads                        # output directory
# DOWNLOAD_WORKERS=4                            # parallel download threads
# DOWNLOAD_MAX_RETRIES=5                        # retry attempts per file
# DOWNLOAD_CHANNELS=                            # channels to download (empty = all)
```

> **Tip:** For machines with 8+ CPU cores, set `DOWNLOAD_WORKERS=12` for faster file downloads (I/O-bound, not CPU-bound).

---

## Migration Workflow

### Step 1: Prepare Channels

```bash
python prepare_channels.py
```

This script:
- Lists all channels (public, private, archived) with a summary
- Unarchives archived channels so the bot can read them
- Invites the bot to every channel

**Requires:** `SLACK_USER_TOKEN` and `SLACK_BOT_USER_ID` in `.env`.

### Step 2: Download File Attachments (Recommended)

```bash
# Scan first, then confirm
python download_files.py

# Skip confirmation (for automation / overnight runs)
python download_files.py -y

# Download from specific channels only
python download_files.py --channels general,engineering

# Dry-run: scan and show summary without downloading
python download_files.py --scan-only
```

This script:
- Scans all channels for Slack-hosted file attachments (skips Google Drive, Dropbox, and other external links)
- Shows a summary table with file counts and sizes per channel
- Downloads files concurrently with retry and resume support
- Writes a `file_manifest.csv` per channel mapping each file to its source message

**State file:** `download_state.json` — tracks completed channels, downloaded files (by `channel/file_id`), and cached scan results.

**Important:**
- Only files **uploaded directly to Slack** are downloaded. External links (Google Drive, Dropbox, etc.) are skipped.
- On re-run, cached scan results are reused. Use `--rescan` to force a fresh scan.
- Files are deduplicated by `file_id` — safe to re-run without duplicates.

### Step 3: Run the Migration

```bash
# Test with one channel first
# Set SLACK_CHANNELS=test-channel in .env, then:
python migrate.py

# Full migration (all channels)
# Set SLACK_CHANNELS= (empty) in .env, then:
python migrate.py
```

For each channel, the migration:

1. Exports all messages and thread replies from Slack
2. Creates a Google Chat Space named `[Migrated] channel-name`
3. Invites channel members to the new space (if `INVITE_MEMBERS=true`)
4. Posts each message in chronological order with author name, timestamp, reactions, and file links
5. Posts thread replies as replies within the correct Google Chat thread
6. Saves progress per-message so it can resume if interrupted

**State file:** `migration_state.json` — tracks migrated channels, in-progress channels (per-message checkpoints), invited/skipped members, and failed channels.

### Step 4: Re-invite Skipped Users (Optional)

If some users were not found in Google Workspace during migration:

```bash
# Dry-run (see who would be re-invited)
python reinvite_skipped.py

# Actually re-invite
python reinvite_skipped.py --apply
```

Useful after new users are provisioned in Google Workspace.

### Step 5: Archive Slack Channels (Optional)

```bash
# Dry-run (see what would be archived)
python archive_migrated.py

# Actually archive
python archive_migrated.py --apply
```

Archives all Slack channels listed in `migration_state.json` as successfully migrated. **Requires** `SLACK_USER_TOKEN` with `channels:manage` scope.

### Step 6: Check External Access (Optional)

```bash
# Check all migrated spaces
python enable_external_access.py

# Check specific channels
python enable_external_access.py web-team marketing
```

Reports which Google Chat spaces allow external users. Note: the `externalUserAllowed` setting on Google Chat spaces is **immutable after creation** — it can only be set when the space is first created and cannot be changed via the API afterwards. The script provides direct links to each space's settings for manual changes.

### (Optional) Convert Private Channels to Public

```bash
# Dry-run
python convert_private_to_public.py

# Convert specific channels
python convert_private_to_public.py --channels secret-project,internal --apply
```

Converts private Slack channels to public so they appear in Slack's standard export. **Requires:** `SLACK_USER_TOKEN` with `admin.conversations:write` scope + Workspace Owner role + Business+ or Enterprise Grid plan.

---

## Scripts Reference

| Script | Purpose | Needs User Token | CLI Flags |
|--------|---------|:---:|------|
| `prepare_channels.py` | Unarchive channels + invite bot | Yes | — |
| `download_files.py` | Download Slack file attachments | No | `--channels`, `--workers`, `--yes`, `--scan-only`, `--rescan` |
| `migrate.py` | Export Slack → import to Google Chat | No | — |
| `reinvite_skipped.py` | Re-invite skipped Google Chat users | No | `--apply` |
| `archive_migrated.py` | Archive migrated Slack channels | Yes | `--apply` |
| `enable_external_access.py` | Check space external access | No | `[channel names]` |
| `convert_private_to_public.py` | Convert private → public channels | Yes | `--channels`, `--apply` |

**Supporting modules** (not run directly):

| Module | Purpose |
|--------|---------|
| `config.py` | Loads and validates `.env` configuration |
| `slack_client.py` | Slack API client — exports channels, messages, threads, files, reactions |
| `gchat_client.py` | Google Chat API client — creates spaces, invites members, posts messages |

---

## Environment Variables

### Required

| Variable | Description |
|----------|-------------|
| `SLACK_BOT_TOKEN` | Bot User OAuth Token (`xoxb-…`) |

### Required for Admin Scripts

| Variable | Used By | Description |
|----------|---------|-------------|
| `SLACK_USER_TOKEN` | `prepare_channels.py`, `archive_migrated.py`, `convert_private_to_public.py` | User OAuth Token (`xoxp-…`) with admin scopes |
| `SLACK_BOT_USER_ID` | `prepare_channels.py` | Bot's Slack User ID (e.g. `U0123456789`) |

### Google Chat Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_AUTH_MODE` | `oauth` | `oauth` (recommended) or `service_account` |
| `GOOGLE_OAUTH_CLIENT_SECRET_PATH` | `./client_secret.json` | Path to OAuth client secret JSON |
| `GOOGLE_SERVICE_ACCOUNT_KEY_PATH` | — | Path to service account key (service_account mode) |
| `GOOGLE_DELEGATED_USER_EMAIL` | — | Admin email for domain delegation (service_account mode) |

### Migration Options

| Variable | Default | Description |
|----------|---------|-------------|
| `SLACK_CHANNELS` | *(empty = all)* | Comma-separated channel names to migrate |
| `SPACE_NAME_PREFIX` | `[Migrated]` | Prefix for new Google Chat space names |
| `GCHAT_RATE_LIMIT` | `2` | Max messages per second to Google Chat |
| `INCLUDE_FILE_LINKS` | `true` | Include file attachment URLs in migrated messages |
| `INVITE_MEMBERS` | `true` | Auto-invite Slack channel members to Google Chat spaces |
| `ARCHIVE_AFTER_MIGRATE` | `false` | Archive Slack channels after successful migration |
| `LOG_LEVEL` | `INFO` | Logging verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

### File Download Options

| Variable | Default | Description |
|----------|---------|-------------|
| `DOWNLOAD_DIR` | `downloads` | Output directory for downloaded files |
| `DOWNLOAD_WORKERS` | `4` | Number of parallel download threads |
| `DOWNLOAD_MAX_RETRIES` | `5` | Max retry attempts per file on network errors |
| `DOWNLOAD_CHANNELS` | *(empty = all)* | Comma-separated channel names to download files from |

---

## State Files & Resume

The toolkit uses JSON state files to track progress. All state writes use atomic file operations (write to temp file → rename) to prevent corruption on crash.

### `migration_state.json`

Created by `migrate.py`. Tracks:

- `migrated_channels` — Successfully migrated channels with space names and message counts
- `in_progress` — Channels currently being migrated with per-message checkpoints (`completed_ts`)
- `invited:<channel>` — Users successfully invited to each space
- `skipped:<channel>` — Users that could not be invited (not found in Google Workspace)
- `failed_channels` — Channels that failed migration (will be retried on next run)
- `archived_channels` — Channels that were archived after migration

**To resume:** Just re-run `python migrate.py` — it skips completed channels and picks up from the last checkpoint.

**To re-migrate a channel:** Remove it from `migrated_channels` in the JSON, then re-run. Note: this creates duplicate messages unless you delete the Google Chat space first.

### `download_state.json`

Created by `download_files.py`. Tracks:

- `completed_channels` — Channels whose files were all downloaded successfully
- `downloaded_files` — Individual files tracked by `channel_name/file_id` with file size for verification
- `scanned_channels` — Cached scan results (file metadata per channel); reused on re-run to avoid API calls

**To resume:** Just re-run `python download_files.py` — it skips completed channels and already-downloaded files.

**To force a fresh scan:** Use `python download_files.py --rescan`.

---

## Verify & Troubleshoot

### Check Migration Results

- Open [Google Chat](https://chat.google.com) → look for spaces prefixed with `[Migrated]`
- Verify messages and threads appear correctly
- Check `migration_state.json` for any `failed_channels`

### Check Downloaded Files

```bash
# See per-channel file manifests
ls downloads/*/file_manifest.csv

# Count total downloaded files
find downloads -type f ! -name "*.csv" ! -name "*.tmp" | wc -l
```

### Common Errors & Fixes

| Error | Cause | Fix |
|-------|-------|-----|
| `SLACK_BOT_TOKEN is not set` | Missing `.env` config | Copy `.env.example` to `.env` and fill in values |
| `SlackApiError: not_in_channel` | Bot not in the channel | Run `prepare_channels.py` or `/invite @Migration Bot` |
| `SlackApiError: missing_scope` | Bot missing permissions | Add the required scope in Slack App settings and reinstall |
| `HttpError 403: insufficient scopes` | OAuth scopes not granted | Delete `token.json` and re-run to re-authorize |
| `HttpError 403: Chat app not found` | Chat API app not configured | Follow "Configure the Google Chat App" step |
| `HttpError 404: Space not found` | Space was deleted externally | Remove channel from `migration_state.json` and re-run |
| `HttpError 429: Rate limit exceeded` | Too many API calls | Lower `GCHAT_RATE_LIMIT` in `.env` (e.g. to `1`) |
| `FileNotFoundError: client_secret.json` | OAuth secret missing | Check `GOOGLE_OAUTH_CLIENT_SECRET_PATH` in `.env` |
| `Service account key creation is disabled` | Org policy blocks SA keys | Use OAuth mode (`GOOGLE_AUTH_MODE=oauth`) |
| `google.auth.exceptions.RefreshError` | Token expired or revoked | Delete `token.json` and re-run |
| `IncompleteRead` / `ConnectionError` | Transient network issue | Script retries automatically (up to 5 attempts with backoff) |
| `admin.conversations:write` errors | Missing plan or role | Requires Workspace Owner + Business+ or Enterprise Grid plan |

### Enable Debug Logging

```ini
# In .env:
LOG_LEVEL=DEBUG
```

---

## Important Notes & Limitations

### Before You Start

- **Test first:** Always test with 1–2 small channels before running a full migration
- **File attachments:** Run `download_files.py` **before** decommissioning Slack — file URLs require an active Slack token
- **Slack export:** The standard Slack export (admin console) only exports public channels. Use `convert_private_to_public.py` if you need private channels in the export
- **Overnight runs:** The toolkit is hardened for long runs — rate limiting, exponential backoff, and per-message checkpointing ensure stability
- **Do not run multiple instances** of the same script simultaneously — state files are not locked for concurrent access

### Limitations

| Limitation | Details |
|-----------|---------|
| **File attachments** | Only links are included in migrated messages, not the actual files. Use `download_files.py` to save files locally before Slack is decommissioned |
| **External files** | Google Drive, Dropbox, and other externally-linked files are skipped by the download script — only Slack-hosted uploads are saved |
| **Formatting** | Rich Slack formatting (blocks, apps, interactive elements) is converted to plain-text approximations |
| **User mapping** | Messages show the Slack user's display name, not their Google Chat identity |
| **Message edits** | Only the latest version of each message is migrated; edit history is not preserved |
| **Custom emoji** | Custom Slack emojis appear as `:emoji_name:` text |
| **External access** | The `externalUserAllowed` setting on Google Chat spaces is **immutable after creation** — it can only be set when the space is first created |
| **Rate limits** | Google Chat API has quotas; very large workspaces may take hours. Default rate is 2 msgs/sec |

---

## Security Notes

- **Never commit** secrets to version control: `.env`, `client_secret.json`, `token.json`, `service_account.json`
- **Revoke tokens** after migration is complete:
  - Slack: Uninstall the app from your workspace
  - Google: Delete or disable the OAuth client and/or service account
- **Read-only on Slack:** The migration tool only reads from Slack (except `archive_migrated.py` and `convert_private_to_public.py`)
- **State files** may contain channel names, user emails, and message timestamps — treat them as internal data
- **Downloaded files** retain original filenames — they may contain sensitive content

---

## Project Structure

```
slack-googlechat-migration/
├── README.md                      # This guide
├── requirements.txt               # Python dependencies
├── .env.example                   # Configuration template
├── .env                           # Your configuration (git-ignored)
├── .gitignore                     # Git ignore rules
│
├── config.py                      # Configuration loader & validator
├── slack_client.py                # Slack API export module
├── gchat_client.py                # Google Chat API import module
│
├── prepare_channels.py            # Step 1: Unarchive + invite bot
├── download_files.py              # Step 2: Download file attachments
├── migrate.py                     # Step 3: Main migration script
├── reinvite_skipped.py            # Step 4: Re-invite skipped users
├── archive_migrated.py            # Step 5: Archive Slack channels
├── enable_external_access.py      # Check space external access
├── convert_private_to_public.py   # Convert private channels to public
│
├── client_secret.json             # OAuth client secret (git-ignored)
├── token.json                     # Cached OAuth token (git-ignored, auto-generated)
├── service_account.json           # SA key if using service_account mode (git-ignored)
├── migration_state.json           # Migration progress (git-ignored, auto-generated)
├── download_state.json            # Download progress (git-ignored, auto-generated)
├── downloads/                     # Downloaded Slack files (git-ignored)
│   └── <channel>/
│       ├── <file_id>_<filename>   # Downloaded file
│       └── file_manifest.csv      # CSV manifest for this channel
└── venv/                          # Python virtual environment (git-ignored)
```
