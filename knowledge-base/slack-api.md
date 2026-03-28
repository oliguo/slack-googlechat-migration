## Bot Token vs User Token Scopes
**Date:** 2026-03-24
**Context:** Archiving and converting channels failed because bot token (xoxb-) was used instead of user token (xoxp-)
**Best Practice:**
- `xoxb-` (bot tokens) cannot: archive channels, convert private→public, manage channel settings
- `xoxp-` (user tokens) are needed for admin operations — must come from a Workspace Owner/Admin
- Key scopes for migration workflows:
  - `channels:manage` + `groups:write` — archive channels
  - `admin.conversations:write` — convert private to public (requires Business+ or Enterprise Grid plan)
  - `channels:read` + `groups:read` — list all channels including private
- Always validate token type at script startup: check prefix is `xoxp-` for admin scripts
- The `admin.conversations.convertToPublic` API only works on paid plans (Business+/Enterprise Grid)
**Keywords:** slack, token, xoxb, xoxp, scope, archive, convert, admin

## Slack Export Only Includes Public Channels
**Date:** 2026-03-24
**Context:** Slack's standard export from the admin console excludes private channels and DMs
**Best Practice:**
- Slack standard export (Settings → Import/Export) only exports public channel messages
- To include private channels in export: convert them to public first using `admin.conversations.convertToPublic`
- This API requires: `xoxp-` user token, `admin.conversations:write` scope, Workspace Owner role, Business+ plan
- If plan doesn't support admin API, channels must be converted manually in Slack UI: channel settings → Change to public
- Always do a dry-run first (`--channels` filter) to verify the conversion works before batch-converting all
**Keywords:** slack, export, private-channel, public-channel, convert, admin-api

## Slack API IncompleteRead Retry Pattern
**Date:** 2026-03-26
**Context:** `conversations.list` and other Slack API calls intermittently fail with `http.client.IncompleteRead` on large workspaces
**Best Practice:**
- Slack SDK only raises `SlackApiError` for HTTP-level errors; network-level errors (IncompleteRead, ConnectionError, timeout) are bare exceptions
- Wrap every Slack API call in a generic retry loop that catches `Exception` (not just `SlackApiError`)
- Use exponential backoff: `2 ** attempt` seconds, up to 5 attempts
- Continue to handle 429 rate-limit errors separately with unlimited retries using the `Retry-After` header
- Pattern: try/except inside a for-loop with `time.sleep(backoff)` on failure; re-raise after max attempts
- This is especially important for paginated calls that make many sequential requests
**Keywords:** slack, IncompleteRead, retry, backoff, network-error, conversations-list

## Slack File Modes — Hosted vs External Uploads
**Date:** 2026-03-28
**Context:** Download script was fetching Google Drive and Dropbox links in addition to actual Slack-hosted files
**Best Practice:**
- Every Slack file object has a `mode` field indicating its type:
  - `hosted` — uploaded directly to Slack (the only type worth downloading)
  - `external` — linked from Google Drive, Dropbox, OneDrive, etc. (skip these)
  - `tombstone` — deleted file placeholder (skip)
  - `snippet` — code snippet (may want to include)
  - `post` — Slack post (may want to include)
- Filter with: `if f.get("mode") == "external": continue` right after the tombstone check
- External files have `url_private_download` pointing to Slack's redirect, not the actual file — downloading them gives HTML error pages
- For a complete backup, consider also skipping `snippet` and `post` modes if only binary attachments are needed
**Keywords:** slack, file-mode, hosted, external, google-drive, download, filter
