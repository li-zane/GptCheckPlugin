# Task Plan

## Goal
Create a runnable sub2api companion plugin that monitors imported GPT accounts, refreshes expired ChatGPT access tokens through a headless Playwright login flow, receives email verification codes through pluggable mail adapters, and exposes a React admin panel protected by an app key.

## Phases

- [x] Phase 1: Inspect workspace and gather sub2api API facts.
- [x] Phase 2: Scaffold FastAPI backend, SQLite models, config, auth, and sub2api client.
- [x] Phase 3: Implement monitor, token refresh orchestration, Playwright login worker, and mail adapter layer.
- [x] Phase 4: Scaffold React frontend with login, account status, mailbox import, settings, and history views.
- [x] Phase 5: Add documentation, env examples, and local run scripts.
- [x] Phase 6: Run backend/frontend checks and fix issues.

## Decisions

- Backend: FastAPI with SQLAlchemy async SQLite, APScheduler-style background loop implemented through asyncio lifespan task.
- Frontend: Vite + React + TypeScript.
- Authentication: single admin key, exchanged for an httpOnly session cookie.
- sub2api integration: configurable base URL and admin token, default paths based on public sub2api frontend API references.
- Mail: pluggable adapter registry; Outlook/Hotmail Graph refresh-token adapter included, custom webhook/manual adapters stubbed through a common interface.

## Risks

- ChatGPT login DOM and flow may change. Use resilient selectors with fallbacks and record refresh failures.
- sub2api account schema can vary by version. Keep raw payload and make email/access-token paths configurable where practical.
- Browser automation may hit anti-abuse or MFA cases. Fail gracefully and surface status in backend and frontend.

## Errors Encountered

| Error | Attempt | Resolution |
| --- | --- | --- |
| Workspace is not a git repository | Initial inspection | Proceed as a fresh project without git operations. |
| Playwright browser executable missing | First UI screenshot attempt | Installed Chromium with `python -m playwright install chromium`, then captured desktop and mobile screenshots successfully. |
| Attempted to stop PID 0 while restarting preview backend | Used a loose port query that returned an Idle/TIME_WAIT owner | Rechecked only `State Listen`; backend health is `ok` on port 8000. |
| PowerShell rejected Bash heredoc syntax | First quick parser verification | Re-ran the parser check with a PowerShell here-string piped into Python. |
| Playwright could not find temporary mailbox row | Imported by browser `fetch` but React state had not reloaded | Reloaded the page before opening the mailbox view, verified the dialog, then deleted the test row. |
| PowerShell rejected Bash heredoc syntax again | Batch import smoke test | Re-ran with a PowerShell here-string and completed the test. |
| Outlook mailbox preview was slow | Benchmarked imported mailboxes and profiled token/IMAP phases | Reordered Outlook mail reads to O2/IMAP first, cached access tokens/strategy, batched IMAP fetches, and added IMAP timeout cooldown. |
| Outlook IMAP became the bottleneck | Compared `mail-manager` and tested Outlook REST with existing imported tokens | Added the `mail-manager` style Outlook REST fast path before Graph/IMAP fallback. |
| Default Python could not import `app` | First live usage refresh smoke script from project root | Re-ran with `PYTHONPATH=backend`. |
| Default Python lacked backend dependencies | Second live usage refresh smoke script | Re-ran the check with `.venv\Scripts\python.exe`. |
| Cleanup endpoint smoke test was not a no-op | Called authenticated `DELETE /api/accounts/deactivated` expecting no deactive rows | It deleted the freshly deactivated `NiaLumsden2003@outlook.com` local/sub2api/mailbox rows; recorded this explicitly in progress and verified remaining state. |
| sub2api server-side account status check timed out upstream | Direct smoke call to `/admin/accounts/3/check-status` | Confirmed the plugin treats this as an AT-path failure and falls back to the existing browser/email login path. |
| Playwright UI text lookup lost Chinese characters through PowerShell encoding | First UI verification for the accounts nav | Re-ran the UI check using Unicode escapes and captured screenshots successfully. |

## Change Request: Runtime sub2api Settings

- [x] Add persisted runtime settings for sub2api base URL/port, x-api key, and monitor interval.
- [x] Add local sub2api port scanning on startup and through an authenticated API endpoint.
- [x] Make monitor/sub2api clients read the latest runtime settings without requiring a restart.
- [x] Add a React settings view for manual port/key/interval edits and scan results.
- [x] Rebuild and compile-check backend/frontend.

## Change Request Notes

- Local sub2api on this machine is listening on `127.0.0.1:18080`; startup scan now detects it through `/api/v1/admin/accounts` returning 401 when no key is supplied.
- `httpx` was receiving a proxy-driven 502 for localhost probes, so sub2api scan/client requests now use `trust_env=False`.

## Change Request: Display Time Zone

- [x] Add a persisted display time zone runtime setting.
- [x] Expose the time zone in the settings API.
- [x] Add a time zone selector to the settings page.
- [x] Apply the selected time zone to all frontend date/time displays.
- [x] Rebuild and smoke-check backend/frontend.

## Change Request: Refresh Reliability and Minimal Credentials Updates

- [x] Route ChatGPT login directly through `/auth/login` and retry email submit when the first click only hydrates the form.
- [x] Wait for the verification-code input before polling the mailbox, and allow a 15-minute code lookup grace window for reused/recent ChatGPT codes.
- [x] Return the full ChatGPT session payload from the browser worker and derive credential changes from session/JWT fields.
- [x] Update sub2api through single-account `bulk-update` so only changed `credentials` keys are merged into the account.
- [x] Clear/recover sub2api state, set `schedulable=true`, and clear temp unschedulable state after a successful refresh.
- [x] Clean up stale queued/running refresh jobs on backend startup.
- [x] Compile-check backend and verify live refresh/sync: latest sync reports `error_seen=0`, `queued=0`.

## Change Request: Post-refresh Usage Refresh

- [x] Use sub2api account usage query after successful credentials updates.
- [x] Call `GET /admin/accounts/:id/usage?source=active&force=true` so sub2api refreshes quota/usage and syncs its passive cache.
- [x] Keep the refresh job successful if the credentials update succeeds but usage refresh fails; record a warning event instead.
- [x] Compile-check backend, restart the running backend, and verify current GPT account usage queries return success.

## Change Request: Deactivated Account Handling and Cleanup

- [x] Inspect recent logs/data for `EdenBeard6250@outlook.com` and imported deactive account `NiaLumsden2003@outlook.com`.
- [x] Trace account sync/monitor/refresh/test-connection paths and confirm which field is used as the account email.
- [x] Mark accounts deactivated when ChatGPT/browser or sub2api responses indicate account deactivation, including during refresh tasks.
- [x] Prevent monitor/error detection from refreshing sub2api accounts that do not expose a usable email in credentials/profile payload.
- [x] Add backend and frontend one-click deletion for deactivated accounts, including local account rows, mailbox rows, and sub2api accounts.
- [x] Rebuild/compile-check and smoke-test the affected API paths.

## Change Request: AT-first Status and Usage Window Automation

- [x] Add a sub2api server-side OpenAI account status endpoint that uses the stored unredacted access token and returns only safe status fields.
- [x] Update plugin refresh jobs to try sub2api AT status first, then plugin-visible AT if available, then the browser/email login refresh path.
- [x] Refresh sub2api usage windows after AT-status or browser-session success.
- [x] Add a manual accounts-page action to query all GPT account usage windows.
- [x] Add persisted automatic usage-window refresh settings and a background loop.
- [x] Prevent the accounts page from automatically forcing an active usage query just by opening the page.
- [x] Set `JadeSanchez2515@outlook.com` usage display fields back to 50% for manual testing.
- [x] Rebuild/compile-check and smoke-test backend service plus local UI.

## Change Request: Lower-memory Protocol Refresh

- [x] Inspect whether HAR parsing is necessary for session refresh.
- [x] Confirm sub2api already exposes a server-side OAuth refresh endpoint backed by its stored `refresh_token`.
- [x] Add a plugin sub2api client method for `POST /admin/accounts/:id/refresh`.
- [x] Insert the protocol refresh step before the Playwright fallback.
- [x] Compile-check and run one-account smoke test with memory peak history.
- [x] Add a ChatGPT Web protocol login path that uses NextAuth/OpenAI email OTP to fetch `/api/auth/session` without Playwright when no RT path is available.

## Recovery: NiaLumsden2003 Accidental Cleanup

- [x] Confirm sub2api account `5` was soft-deleted, not physically removed.
- [x] Restore sub2api account `5` and keep it unschedulable/deactive.
- [x] Recover local encrypted mailbox credential row from SQLite free page 42.
- [x] Fix error-message deactivation classification for error/unschedulable accounts.
- [x] Restart backend and verify safe sync reports `queued=0`.

## Incident: sub2api Service and Mailbox Recovery

- [x] Confirm which local services are actually down versus only unauthenticated.
- [x] Inspect local SQLite mailbox/account state without printing secrets.
- [x] Check whether missing `.env` or encryption-key mismatch is hiding mailbox rows.
- [x] Recover mailbox/account data from local DB/sub2api storage if rows were deleted.
- [x] Restart affected services and verify UI/API health.
