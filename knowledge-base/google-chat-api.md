## Google Chat `externalUserAllowed` Is Immutable
**Date:** 2026-03-24
**Context:** Attempted to enable external user access on existing Google Chat spaces via `spaces.patch()` — got HTTP 400
**Best Practice:**
- `externalUserAllowed` is documented as **Immutable** in the Google Chat API — it can only be set at space creation time
- `spaces.patch()` does NOT support `external_user_allowed` in the updateMask — returns `Invalid update mask`
- Adding `"externalUserAllowed": True` to `spaces().create(body=...)` returns **403 PERMISSION_DENIED** if the OAuth credential lacks org-level permission to create external-access spaces
- For existing spaces, the only way to enable external access is manually via the Google Chat UI
- Workaround: create a checker script that lists spaces and outputs direct Google Chat URLs for manual update
**Keywords:** google-chat, externalUserAllowed, immutable, spaces-patch, 400, 403

## Lambda-Based Retry for Google API Requests
**Date:** 2026-03-24
**Context:** Retry logic for Google Chat API calls failed because `HttpRequest.execute()` consumes the request object
**Best Practice:**
- `googleapiclient`'s `HttpRequest.execute()` consumes the request — calling it again on retry raises errors or sends a malformed request
- Always wrap API calls in a lambda: `lambda: self._service.spaces().create(body=body).execute()`
- The retry function calls `api_call()` each time, which creates a fresh `HttpRequest` per attempt
- This applies to ALL Google API client library calls that need retry (create, list, get, etc.)
**Keywords:** google-api, retry, lambda, HttpRequest, consumed-request, googleapiclient
