## Resumable Migration with Atomic State Persistence
**Date:** 2026-03-24
**Context:** Long-running migration (176+ channels) that can crash or be interrupted overnight
**Best Practice:**
- Use atomic writes: write to temp file with `tempfile.mkstemp()`, then `os.replace()` to final path — prevents corruption on crash
- Track per-message progress in `in_progress` dict with `completed_ts` sets for each channel
- Batch state saves every N messages (`SAVE_INTERVAL = 10`) to reduce I/O while limiting replay on crash
- On resume, skip messages whose `ts` is in `completed_ts`; move channel from `in_progress` → `migrated_channels` only when fully done
- Always save state immediately after completing a channel (don't batch the final save)
**Keywords:** migration, state, resume, atomic-write, checkpoint, crash-recovery

## Graceful Permission Degradation Pattern
**Date:** 2026-03-24
**Context:** Slack archive operation fails with permission errors mid-migration; don't want to stop the whole run
**Best Practice:**
- Use a module-level flag (e.g., `_archive_disabled = False`) that flips to `True` on first permission error
- Check the flag at the start of each call — return immediately if disabled
- Catch specific error codes: `missing_scope`, `restricted_action`, `not_allowed_token_type`
- Log a clear message telling the user exactly what token/scope is needed
- The rest of the migration continues unaffected
**Keywords:** permission, graceful-degradation, flag, error-handling, slack, archive

## Scan-Cache Resume Pattern for Large Downloads
**Date:** 2026-03-26
**Context:** Re-running a download script after interruption re-scans all Slack channels via API, wasting minutes on large workspaces
**Best Practice:**
- After scanning each channel, serialize the results (file records) into the state JSON under `state["scanned_channels"]`
- On re-run, check for cached scan data first — restore it instantly without any API calls
- Provide a `--rescan` CLI flag to force a fresh scan when needed (e.g., new files uploaded since last scan)
- Clean the scan cache from state after all downloads complete successfully (stale cache is worse than no cache)
- For serialization, convert dataclass/namedtuple records to dicts with `_record_to_dict()` and back with `_record_from_dict()` helpers
- Cache per-channel (not globally) so partial scans are still useful after interruption
**Keywords:** scan-cache, resume, download, state, serialization, performance

## 3-Phase CLI Pattern: Scan → Summary → Confirm
**Date:** 2026-03-26
**Context:** Users want to know what a long-running script will do before committing — especially for bulk downloads or migrations
**Best Practice:**
- Phase 1 (Scan): Gather all work items without performing mutations; show progress per channel
- Phase 2 (Summary): Print a formatted table with per-channel breakdown (counts, sizes, already-done vs new)
- Phase 3 (Confirm): Prompt `Proceed? [y/N]` before executing; support `--yes`/`-y` flag to skip for automation
- Add `--scan-only` flag for dry-run mode that exits after Phase 2
- Show totals: "X files across Y channels, Z already downloaded, W to download (N GB)"
- Use simple aligned columns (`f"{name:<30} {count:>6}"`) rather than heavy table libraries
**Keywords:** CLI, scan, confirm, dry-run, UX, summary-table

## JSON State Safety — Avoid User Content in Keys and Cache
**Date:** 2026-03-28
**Context:** Download state JSON broke because filenames with special characters were used as keys, and cached `message_text` contained unescaped characters
**Best Practice:**
- Use stable, simple IDs as state keys: `channel_name/file_id` — never include filenames, message text, or other user-generated content
- Strip free-text fields (e.g., `message_text`) from serialized cache entries; they add no resume value and risk JSON corruption
- Keep `_record_to_dict()` / `_record_from_dict()` in sync: if a field is removed from serialization, default it in deserialization (`d.get("field", "")`)
- Test with channels that have emoji, unicode, slashes, quotes in names/messages before running at scale
**Keywords:** JSON, state, key-format, serialization, corruption, safety
