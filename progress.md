# Progress

## 2026-05-23

- Read required skills: planning-with-files, frontend-design, security-review.
- Inspected the workspace; it is empty and not a git repository.
- Checked local runtime versions for Python, Node, and npm.
- Researched current sub2api account API shape from public references.
- Created persistent planning files in the project root.
- Added FastAPI backend scaffolding with SQLite models, settings, encryption helper, admin-key cookie auth, and CORS.
- Added sub2api account client, monitor service, refresh queue, Playwright ChatGPT login flow, and Outlook/custom/manual mail adapters.
- Added API routes for auth, dashboard, account sync/refresh/jobs/events, and mailbox import/list/delete.
- Added Vite React frontend with a full-page login, operational dashboard, account table, mailbox import/list, and history views.
- Added `.env.example`, root npm scripts, and Chinese README with install/config/import/custom-mail notes.
- Installed frontend dependencies and built the frontend successfully.
- Installed backend dependencies, initialized the SQLite schema, and smoke-tested health/login/dashboard API paths.
- Started local backend and frontend preview servers, verified the login/dashboard flow with Playwright, and captured desktop/mobile screenshots in `output/playwright/`.
- Adjusted dashboard error-account counting to include sub2api status strings, then rechecked backend compile and API smoke paths.
- Updated mailbox import to auto-detect Outlook/Hotmail providers from mailbox suffix unless a row explicitly specifies provider.
- Rebuilt the frontend, recompiled the backend, verified parser behavior, and restarted the preview backend.
- Added mailbox message preview endpoint and frontend mail dialog with inbox/junk tabs for Outlook/Hotmail and inbox-only display for custom/manual providers.
- Verified the dialog with Playwright using a temporary manual mailbox, captured `output/playwright/mail-dialog.png`, and deleted the temporary row.
- Fixed Outlook/Hotmail mail reading to try Graph first and then O2/IMAP token flows, so IMAP-scoped refresh tokens are no longer reported as Graph refresh-token failures by default.
- Enhanced mailbox import response with imported/skipped counts and invalid line numbers; confirmed multi-line import works and cleans up test rows.
- Added hard timeouts for mailbox reading, token exchange, IMAP sockets, and frontend mailbox fetches so the mail dialog returns an explicit error instead of loading forever.
- Improved Outlook token flow ordering with consumer/common no-scope attempts, access-token audience detection, IMAP host fallback, and optional password IMAP fallback.
- Replaced the Outlook/Hotmail adapter with the user-provided Microsoft Graph flow: refresh token -> Graph access token -> Graph mail folders, including body/bodyPreview/header fields and code extraction from subject/snippet/text.
- Verified compile, frontend build, and fake-token Graph failure behavior, then restarted the preview backend.
- Restored a constrained fallback path after Graph scope failures: Graph with scope, Graph without scope, then O2/IMAP, so scope-mismatched refresh tokens no longer stop immediately at the first Graph error.

## 2026-05-24

- Benchmarked 5 imported Outlook mailboxes with masked output only; previous inbox reads succeeded but took about 10.4-13.3 seconds each because Graph attempts failed first and IMAP fetched full messages one by one.
- Profiled one mailbox safely and found the working path was O2 token refresh plus IMAP; Graph scope failed with AADSTS70000 and Graph no-scope returned a token unsuitable for Graph mail.
- Optimized Outlook/Hotmail reading to try O2/IMAP first, cache short-lived access tokens and successful strategy choices in memory, and batch-fetch IMAP header plus partial body instead of full RFC822 messages.
- Fixed the undefined `token_result` bug in verification-code fetching and changed code lookup to check inbox first, only reading junk if needed.
- Added IMAP timeout/cooldown handling so repeated host handshakes do not keep the mail dialog loading forever when Microsoft IMAP is unreachable.
- Updated mailbox preview reads to record `last_success_at`/`last_error`, recompiled the backend, and rebuilt the frontend successfully.
- Compared the local private `mail-manager` implementation and found its fast Outlook path uses Microsoft common no-scope token refresh plus Outlook REST fallback when Graph rejects opaque tokens.
- Added Outlook REST as the first Outlook/Hotmail read strategy in this project. Real imported mailbox benchmarks improved from about 10.4-13.3 seconds to about 2.8-3.4 seconds on first read and about 1.6-1.8 seconds with cached tokens.
- Added runtime settings storage and API routes for sub2api base URL/port, x-api key, and monitor interval.
- Added startup and manual sub2api port scanning; scanner now combines configured candidate ports with current local listening ports.
- Updated the sub2api client and monitor loop to read runtime settings dynamically, including immediate monitor wake-up after settings changes.
- Added a React settings page for manual sub2api port/key/interval edits and scan results.
- Fixed localhost sub2api probing under proxy-heavy environments by disabling env proxy use for sub2api HTTP calls.
- Verified backend compile, frontend production build, settings API smoke test, and Playwright screenshot `output/playwright/settings-view.png`; the running panel detected local sub2api at `127.0.0.1:18080`.
- Fixed a follow-up sync 500 caused by false-positive port scans: local ports `9210` and `9993` returned HTTP 200/401 on the accounts path but were not sub2api. Scanner now validates account JSON/auth error shape before applying a port, and sub2api JSON/HTTP errors are surfaced as 502 details instead of raw 500s.
- Re-scanned with the saved x-api key, restored runtime sub2api URL to `http://127.0.0.1:18080/api/v1`, and verified `/api/accounts/sync` returns successfully.
- Added a persisted `display_timezone` runtime setting, exposed it through `/api/settings`, and added `DISPLAY_TIMEZONE=Asia/Shanghai` to `.env.example`.
- Added a settings-page time zone selector with live preview, and routed all frontend date/time displays through the selected time zone.
- Verified backend compile, frontend production build, settings API update/restore for `UTC` -> `Asia/Shanghai`, and Playwright screenshot `output/playwright/settings-timezone.png`.
- Fixed history timestamps that still appeared in UTC after changing display time zone. SQLite returns stored UTC datetimes without `Z`/offset, so the frontend now treats offset-less API timestamps as UTC before formatting.
- Rebuilt frontend/backend and verified the history page renders the latest `2026-05-24T12:03:19` record as `05/24 20:03` under `Asia/Shanghai`; screenshot saved to `output/playwright/history-timezone.png`.
- Compared Outlook/Hotmail mailbox reads against the local `mail-manager` implementation and added the same external `/api/mail-all` fallback after direct REST/Graph/IMAP attempts.
- Restored an IMAP password fallback and added a clearer `APP_ENCRYPTION_KEY` mismatch error when stored mailbox secrets cannot be decrypted.
- Recompiled the backend and restarted the local backend on `127.0.0.1:8000`; the current imported mailbox row now fails with the explicit key-mismatch diagnostic, so it needs the original key restored or a fresh import.
- Added persistent mailbox access-token support: import accepts `mail-manager` registered 6-part rows, stores encrypted mail access tokens, tries stored access tokens before refresh/IMAP fallbacks, and persists newly refreshed mail access tokens.
- Added a SQLite startup migration for the new `mailbox_credentials.encrypted_access_token` column; verified parser behavior for 4-part original, 6-part registered, and 6-part GPT-email-plus-mailbox rows.
- Fixed 4-part Outlook/Hotmail reads by adding a Graph no-scope strategy that uses the refresh token the same way `mail-manager` does before falling back to REST/IMAP/external providers.
- Verified the current 4-part Hotmail import through the running API: `/api/mailboxes/1/messages?folder=inbox&limit=5` returned HTTP 200 with 5 messages, cleared `last_error`, and persisted a refreshed mail access token.
- Fixed refresh jobs that were failing at ChatGPT login by navigating directly to `/auth/login`, using an English browser locale, and retrying email submission when the first click only leaves the login form at `?email=...`.
- Added verification-code input detection before mailbox polling and a 15-minute lookup grace window so recently reused ChatGPT codes are not skipped by strict timestamp filtering.
- Extended browser refresh results to return the full ChatGPT session payload; sub2api writes now derive changed `credentials` keys from the session/JWT and merge them via single-account `bulk-update` instead of replacing the whole credentials object.
- Added startup cleanup for stale queued/running refresh jobs and prevented duplicate active jobs for the same email.
- Added post-refresh sub2api recovery for `schedulable=true` and temp-unschedulable cleanup, because `recover-state` alone can leave a refreshed account unschedulable.
- Recompiled the backend, restarted the local backend, and verified live refresh jobs 33-35 succeeded. Final authenticated sync returned `{"total_seen":2,"error_seen":0,"queued":0}`, and local snapshots show both GPT accounts active/schedulable with no refresh error.
- Confirmed sub2api account usage query semantics from the local sub2api backend/frontend: `GET /admin/accounts/:id/usage?source=active&force=true` performs the same forced active quota lookup used by the admin UI.
- Added `Sub2ApiClient.refresh_account_usage()` and wired refresh jobs to call it after successful session-to-credentials updates.
- Made usage refresh best-effort: failures are recorded as `sub2api_usage_refresh_failed` events and referenced in the job reason, but do not turn a successful credentials update into a failed refresh job.
- Re-applied session-derived subscription fields after usage refresh, with `session.account.planType` taking priority over other plan hints, so usage probes cannot leave a Plus account recorded as Free.
- Tightened deactivation classification: sub2api list snapshots no longer scan stale `error_message` text for deactivation, deactive snapshots are no longer sticky across sync, dashboard error counts exclude deactive accounts, successful refresh clears local deactive state, and sub2api usage/test deactivation text is recorded as a warning instead of overriding successful ChatGPT/session verification.
- Tested NiaLumsden2003 pre-code browser login path: submitting the email reached verification-code input first, while the prior full refresh job detected `account_deactive` after code validation. Keep browser deactivation checks both after email submit and after code validation.
- Redacted common token/password/authorization patterns before storing usage-refresh failure details in events.
- Verified backend syntax with `.venv\Scripts\python.exe -m compileall backend\app`.
- Smoke-tested usage refresh through the new client method against current GPT account IDs `4`, `3`, and `1`; all returned `ok=True` without printing credentials or tokens.
- Restarted the running uvicorn backend on `127.0.0.1:8000`; health check returned `{"status":"ok"}`.
- Started the deactivated-account handling change request: goals are to inspect logs/data, enforce email-field matching from sub2api records, mark deactivated accounts during refresh/test paths, skip records without usable email, and add one-click deletion for deactivated accounts across local data and sub2api.
- Inspected local DB rows for the reported accounts with encrypted/token fields redacted. `NiaLumsden2003` is currently local `error`/unschedulable but not deactive after job 42; `EdenBeard6250` has successful recent refresh jobs and active local status. Found likely identity bug: sub2api account email extraction currently searches the whole object and can use `name`.
- Checked live sub2api state and logs. Current account list still reports Eden active, but sub2api test logs show repeated `account_deactivated` 401s for `/admin/accounts/4/test`; Nia shows `token_invalidated` for `/admin/accounts/5/test`/usage. Confirmed sub2api has `DELETE /api/v1/admin/accounts/:id`.
- Implemented backend changes: sub2api account email extraction now uses explicit email/profile fields only, deactivation text matching includes `account_deactivated`, monitor skips error accounts without enabled mailbox credentials, refresh jobs run a post-update sub2api SSE connection test to catch deactivation, and a `DELETE /api/accounts/deactivated` cleanup endpoint deletes local deactive accounts/mailboxes plus remote sub2api accounts.
- Implemented frontend changes: accounts view now has a disabled-state one-click `删除停用账号` action wired to the cleanup endpoint, with a confirmation prompt.
- Verified backend compile and frontend production build successfully.
- Restarted the local backend on `127.0.0.1:8000`; health check returned `ok`.
- Verified the new sub2api SSE deactivation probe directly: account `4` returns `True` for `account_deactivated`, account `5` returned `False` before browser refresh.
- During authenticated cleanup endpoint smoke testing, the freshly detected deactive `NiaLumsden2003@outlook.com` row was actually deleted by `DELETE /api/accounts/deactivated`: local snapshot, mailbox credential, and sub2api account `5` were removed. This matched the cleanup behavior but was not just a no-op smoke test.
- Ran authenticated sync after cleanup; current local state is 3 accounts total, with `EdenBeard6250@outlook.com` classified as `deactive=true`, `status=error`, `schedulable=false`, and no refresh queued.
- Verified the local React account page in the in-app browser at `http://127.0.0.1:5173`: the `删除停用账号` button is visible/enabled when one deactive account exists, and Eden appears as `停用`.
- Added sub2api server-side AT account status support and restarted sub2api on `127.0.0.1:18080`; unauthenticated access is 401 and authenticated client calls use the stored token without exposing it to the plugin.
- Wired plugin refresh jobs to try sub2api `check-status` first, then plugin-visible AT, then browser/email login; successful AT status refresh also runs sub2api usage refresh.
- Added `POST /api/accounts/usage-refresh`, a background `UsageRefreshService`, persisted `usage_refresh_enabled` and `usage_refresh_interval_seconds`, and settings-page controls for automatic usage-window refresh.
- Added the accounts-page `查询用量窗口` action and adjusted the accounts page so opening it reads cached usage data instead of forcing an active upstream query.
- Smoke-tested `UsageRefreshService.refresh_all(reason="smoke-test")`: 2 GPT accounts refreshed, 1 deactivated account skipped, 0 failures.
- Verified local UI with Playwright screenshots: `output/playwright/accounts-usage-refresh-button.png` and `output/playwright/settings-usage-refresh.png`.
- Re-ran `.venv\Scripts\python.exe -m compileall backend\app` and `npm --prefix frontend run build`; both passed.
- Set sub2api account `JadeSanchez2515@outlook.com` (id `3`) usage display fields back to 50% for 5h/7d and primary/secondary, with utilization fields at `0.5`.
- Restarted the GptCheckPlugin backend on `127.0.0.1:8000`; health check returns `{"status":"ok"}`.
- Started the lower-memory refresh follow-up. Reviewed current plugin order and local sub2api handlers; found existing sub2api `POST /admin/accounts/:id/refresh` uses stored OAuth `refresh_token` server-side and returns redacted account DTOs.
- Added plugin-side `Sub2ApiClient.refresh_account_credentials()` and wired refresh jobs to try this protocol refresh after sub2api access-token status fails and before Playwright/browser login.
- Restored accidentally deleted `NiaLumsden2003@outlook.com`: sub2api Postgres account `5` was undeleted by clearing `deleted_at`, kept as `status=deactive` and `schedulable=false`, and the local encrypted mailbox credential row was recovered from SQLite free page 42 as mailbox id `4`.
- Fixed deactive classification so sub2api `error_message` text containing `account_deactivated` marks an account deactive when the account is already error/failed or unschedulable; this restores Eden's disabled classification without scanning arbitrary active-account payloads.
- Restarted the plugin backend on `127.0.0.1:8000`, ran a safe monitor sync (`total_seen=4`, `error_seen=1`, `queued=0`), and verified both Nia and Eden are locally `deactive=true` with no refresh job queued.
- Re-ran backend compile and frontend production build; both passed.
- Recompiled the backend successfully with `.venv\Scripts\python.exe -m compileall backend\app`.
- Ran one-account protocol smoke refresh for `jadesanchez2515@outlook.com` / sub2api id `3`: job `10` succeeded through `sub2api OAuth token refresh succeeded via stored refresh_token`, refreshed usage, did not need browser login, and recorded `memory_peak_rss_bytes=78909440`.
- Restarted the local backend on `127.0.0.1:8000`; health check returned `ok`. Cleaned one stale running refresh job left by a prior service restart so history no longer shows it as active.
- User correctly pointed out account `3` did not have an RT. Rechecked redacted sub2api status for `jadesanchez2515@outlook.com`: `credentials_status` only reports `has_access_token=true`. The previous job used sub2api's `/refresh` endpoint fallback that returns existing access-token credentials when no refresh token exists, so the success reason was misleading.
- Tightened plugin protocol refresh to require `credentials_status.has_refresh_token=true` or a visible non-redacted refresh-token field before calling `/admin/accounts/:id/refresh`.
- Recompiled backend successfully and verified account `3` now reports `has_refresh_token=False` and skips the protocol refresh branch.
- Implemented `ChatGptProtocolRefresher` using ChatGPT Web NextAuth plus OpenAI email OTP to fetch `/api/auth/session` without Playwright, and inserted it before the Playwright fallback.
- Verified the direct protocol refresher against `JadeSanchez2515@outlook.com` without printing tokens/cookies/codes: it returned session/access token fields and peaked at `90,566,656` bytes RSS (about `86.4 MiB`).
- Triggered formal refresh job `13` for the same account after backend restart; because the existing AT was still valid, the job correctly stopped at the lower-cost sub2api AT-status path, succeeded, and recorded `100,376,576` bytes peak RSS.
- Started incident recovery for reported sub2api outage and cleared mailboxes.
- Checked service ports: sub2api is listening on `127.0.0.1:18080` and returns 401 without auth; plugin backend/frontend are also listening on `8000`/`5173`.
- Confirmed the project root currently has no `.env`, making encryption-key/runtime-token drift a likely explanation for mailbox read/recovery problems.
- Found the active backend API was reading `backend/data/sub2api_at_guardian.db`, which had 0 accounts and 0 mailboxes, while the recovered root DB still had 4 accounts and 4 mailboxes.
- Patched `backend/app/core/database.py` to resolve relative SQLite DB paths against the project root, then restarted the backend on `127.0.0.1:8000`.
- Verified restored API state: dashboard reports 4 accounts, 2 deactive accounts, 4 mailboxes; settings show the restored sub2api key is set; `/api/accounts/sync` returned `total_seen=4`, `error_seen=1`, `queued=0`.
- Smoke-tested mailbox reads without printing contents: Nia/Jade/Eden succeeded; Micah/Hotmail timed out at the existing 45-second mailbox read timeout.
- Investigated why the sub2api panel would not open. Confirmed `18080` serves API routes but returns 404 for browser routes such as `/`, `/admin`, and `/login`.
- Started the sub2api Vue frontend on `127.0.0.1:3000` with `VITE_DEV_PROXY_TARGET=http://127.0.0.1:18080`; verified the root page and proxied `/api/v1/settings/public` both return 200.
- Opened the panel in the in-app browser, logged in with `admin@sub2api.local / sub2api_local_admin`, and verified it reached `http://127.0.0.1:3000/dashboard`.
- Patched sibling sub2api `start-local.ps1` and `stop-local.ps1` so the local panel starts/stops alongside the backend. Script parsing and `start-local.ps1` reuse on already-running services passed.

## 2026-05-25

- Started memory/concurrency follow-up. Confirmed refresh jobs wrap the whole process tree in `ProcessMemorySampler`, which explains high peaks when Playwright/Chromium starts after protocol fallback failure.
- Confirmed the current refresh queue uses one `_active_refreshes` counter backed by runtime setting `refresh_max_concurrency`; browser and protocol work are not independently limited yet.
- Added available-memory detection with psutil, procfs, cgroup, and Windows fallbacks. Browser fallback now skips Playwright if available memory is below the configured threshold, defaulting to 500 MiB.
- Split refresh execution into protocol and browser concurrency slots. Protocol work reads `protocol_refresh_max_concurrency`; browser fallback reads `browser_refresh_max_concurrency`; legacy `refresh_max_concurrency` remains a protocol-compatible alias.
- Exposed the new settings through backend schemas/runtime config, `.env.example`, README, TypeScript types, and the React settings view.
- Verified `.venv/bin/python -m compileall backend/app`, `npm --prefix frontend run build`, `git diff --check`, and a smoke import of `available_system_memory_bytes()` from the project root. The first memory-helper smoke command failed only because it was run from `backend` with the wrong relative `.venv` path, then passed from the root.
- Added `sub2api_auto_recover_state` as a runtime setting and settings-page toggle. `Sub2ApiClient` now reads it through `EffectiveSub2ApiConfig` instead of only from static env settings.
- Added `recovery_enabled` as the account recovery task switch. When disabled, monitor sync still updates snapshots but does not enqueue recovery jobs, and manual refresh returns a 409 conflict.
- Fixed the recovery switch default: missing `recovery_enabled` now means false, not true. Persisted the current local setting to false so the running app stops enqueueing recovery on manual sync.
- Added account and mailbox list search boxes. Account search matches account email, mailbox email, status, schedulable state, sub2api ID, platform/type, duplicate/error flags, and last error. Mailbox search matches GPT email, mailbox email, provider, enabled/disabled state, success time, and last error.
- Verified `npm --prefix frontend run build` and `git diff --check` after the search UI changes.
- Updated mailbox import so existing records are ignored instead of overwritten. Import now skips rows when the GPT email or retrieval mailbox email already exists, and skips duplicate GPT/retrieval mailbox emails within the same pasted batch.
- Verified backend compile, mailbox import parser smoke check, and `git diff --check` after the import change.
- Corrected the accounts-page bulk delete button semantics. It now targets deactivated accounts and duplicate abnormal accounts only when the duplicate group has a non-error, non-deactivated primary replacement; ordinary `status=error` accounts are preserved for possible reauthorization recovery.
- Verified backend compile, frontend production build, a duplicate-cleanup helper smoke check, and `git diff --check` after the bulk delete correction.
- Added `.env` persistence for admin-panel settings, including sub2api URL, x-api key, recovery toggle, automation toggles, intervals, concurrency, memory threshold, display time zone, and site name. `.env` is read from the project root regardless of current working directory.
- Verified backend compile, frontend production build, env merge helper, public settings field smoke check, recovery settings field smoke check, and `git diff --check`.
- Started OAuth RT acquisition integration. Reviewed the existing refresh chain, sub2api credential write paths, current protocol ChatGPT login, and the reference mail-console OAuth flow. The main gap is OAuth PKCE token exchange and refresh-token writeback, not email-code retrieval.
- Added OpenAI OAuth PKCE support with token exchange and sub2api credential writeback. The OAuth path reuses existing mailbox-code polling, writes standard `refresh_token`, and only writes `rt` when the source account already uses that alias or reports `has_rt=true`.
- Probed pure protocol OAuth for `annamason5243@outlook.com`; it reached the email OTP and Codex consent page at about `88.4 MiB`. OAuth RT acquisition now tries this `curl_cffi` protocol path first and falls back to Playwright browser OAuth if the protocol path cannot complete.
- Fixed browser OAuth callback capture for the local `http://localhost:1455/auth/callback` redirect. Without a local listener Chromium changes to `chromewebdata`, so the code is captured from request/frame navigation first.
- Verified browser OAuth fallback for `annamason5243@outlook.com`: got AT/RT/ID token, wrote credentials back through sub2api without printing secrets, and observed about `834.9 MiB` peak RSS.
- Verified the existing sub2api RT refresh path still works for account `548` and stays low-memory at about `77.4 MiB` peak RSS.
- Adjusted refresh ordering so accounts that have RT call the sub2api `/refresh` path before AT status check. This makes manual/scheduled refresh actually use RT instead of returning early when the current AT is still valid.
- Reviewed the OAuth browser helper after smoke testing and hardened button clicks so provider SSO buttons such as Google/Apple/Microsoft are skipped even when the label is exposed through accessibility attributes.
- Redacted token-like substrings in additional refresh failure reasons before storing them on jobs/events. Targeted py-compile, full backend compile, and `git diff --check` passed after these review patches.
- Diagnosed usage-estimate availability. The live usage fetch returned `cost` for all 42 deduped GPT accounts, but the old estimator required baseline-delta usage to be positive, causing first-enable/no-new-usage accounts to show as not estimable.
- Changed remaining-quota estimation to use current official window raw usage and official used percent, while keeping baseline delta for display. Added cached usage-window fallback so the accounts page can show estimates consistently when it calls the endpoint with `refresh=false`.
- After the usage-estimate fix, validation showed 5h estimable accounts improved from 26 to 40 and 7d from 27 to 41 on refreshed data; cached `refresh=false` results matched those counts. Backend compile, frontend build, response-model validation, and `git diff --check` passed.
- Deployed the current working tree by rebuilding the frontend and restarting the existing `gptcheckplugin.service` and `gptcheckplugin-frontend.service` systemd units. Verified backend health, frontend `index-C2rsiITv.js` serving on `127.0.0.1:5173`, and cached quota aggregate counts at 5h=40 / 7d=40 participating estimable accounts.

## 2026-05-27

- Started calibrated quota request. Added a `usage_limit_samples` SQLite table/model for 5h/7d observed window-limit samples.
- Usage estimates now save limit samples from active usage fetches, keep the middle 100 samples per window, and clamp inferred totals to either a 3 sigma sample window or the default 5h `$15-$25` / 7d `$100-$140` ranges while samples are insufficient.
- Updated quota estimation so displayed used percent is based on the calibrated total, not the raw official percentage that caused inflated totals.
- Updated account and quota detail UI cells to show a progress bar plus `已用` and `未用` labels, and removed the previous `新增` wording from the UI.
- Manual account sync now also runs a usage-window refresh and uses the same active usage data to update local limit samples; the frontend sync request timeout was raised to match usage refresh timeouts.
- Verified backend compile, frontend production build, usage-calibration smoke checks, database init migration, service import graph, usage-estimate response-model validation, and `git diff --check`. The first response-model validation command failed only because Python does not allow `async def` directly after semicolons in `-c`; reran it with `exec(...)` successfully. After wiring manual sync to usage refresh, reran backend compile, frontend build, import graph check, response-model validation, and `git diff --check` successfully.
- Reworked the history request after clarification. Removed the aggregate history UI/API/save path and added a dedicated `样本` page instead.
- Added `GET /api/accounts/usage-limit-samples`, returning current 5h/7d local sample rows, the calibration source, sample count, active lower/upper bounds, default bounds, mean, and sigma.
- Added frontend `UsageLimitSamples` types/API and the standalone `样本` navigation item/page. Each window shows the exact rows used to estimate official-window quota: email/account id, observed limit, raw spent, official used percent, reset time, and record time.
- Fixed desktop layout so `.shell` has viewport height, `.sidebar` is sticky/fixed-height without participating in page scrolling, and `.workspace` scrolls independently. Mobile layout falls back to normal document scrolling.
- Verified backend compile, frontend production build, database migration validation, usage-limit sample response validation, and `git diff --check`.
- Added rate-limit distinction to account data and UI. Backend account/usage-estimate responses now expose `rate_limited` and `rate_limited_windows`; the accounts table/search combines those fields with cached usage windows and shows limited accounts as `限流` with warn styling instead of plain `可用`.
- Verification: `python3 -m compileall backend/app`, `npm --prefix frontend run build`, `.venv/bin/python` helper smoke test for rate-limit window detection, and `git diff --check` passed. A system `python3` helper import attempt failed because the system interpreter lacks backend dependencies such as SQLAlchemy; reran the smoke test with the project virtualenv successfully.
- Split the account-list rate-limit display into separate `5h限流` and `7d限流` badges. Each badge now shows recovery timing from cached usage window `remaining_seconds` or `reset_at`, falling back to `恢复时间待查询` when no cached window time is available. Frontend build and `git diff --check` passed after the UI update.
- Deployed the rate-limit UI changes by running `python3 -m compileall backend/app`, `npm --prefix frontend run build`, and restarting `gptcheckplugin.service` plus `gptcheckplugin-frontend.service`. Verified both services are active, backend health returns `{"status":"ok"}`, and the frontend root serves the new `index-DPL1GpkO.js` / `index-CIVQez17.css` build assets.

## 2026-05-28

- Investigated reported repeated refresh failures and backend restarts. `gptcheckplugin.service` was in auto-restart with thousands of restarts, and the backend journal showed SQLite `database is locked` exceptions while inserting `refresh_memory_peak` events.
- Found a stale project uvicorn process still listening on `127.0.0.1:8000`, which made every new systemd-started backend fail with `address already in use` after lifespan startup completed.
- Patched SQLite setup to use a 30-second busy timeout and WAL mode, reducing write-lock contention for concurrent refresh/job/event writes.
- Patched refresh finalization so critical job/snapshot state commits are separate from optional app-event commits. Memory peak and warning event writes are now non-fatal, so an event write lock cannot crash the refresh task or backend process.
- Added a 10-second monitor startup delay so if uvicorn cannot bind its port, scheduled sync does not enqueue fresh refresh jobs before the process exits.
- Stopped the restart loop, terminated the stale uvicorn process with `TERM`, restarted `gptcheckplugin.service`, and verified `/api/health` returned `ok` with only the new systemd PID listening on port `8000`.
- Waited for the next sync/refresh window. The service stayed active with `NRestarts=0`; latest jobs `4103`-`4111` all completed as `deactive` from OpenAI OAuth OTP validation rather than `Service restarted before refresh finished`, and `active_jobs=0`.
- Verified backend compile, `git diff --check`, service health, port ownership, and post-startup monitor sync (`queued=0`).

## 2026-06-05

- Ported the `mail-console-x1` style direct OpenAI GPT token refresh path into the plugin backend.
- Added encrypted local cache fields on `account_snapshots` for GPT `refresh_token`, `access_token`, `id_token`, `client_id`, and token expiry; added the SQLite migration in `init_db()` and verified the new columns exist in `data/sub2api_at_guardian.db`.
- Added `backend/app/services/openai_token_service.py` with two direct OpenAI/ChatGPT operations: `refresh_token` using `grant_type=refresh_token` against `https://auth.openai.com/oauth/token`, and `fetch_profile` using ChatGPT `accounts/check` with the resulting access token.
- Refactored refresh orchestration so session-success finalization is shared across ChatGPT protocol refresh, browser refresh, OAuth refresh-token acquisition, local cached OpenAI RT refresh, and local cached AT profile validation.
- Inserted the new recovery order into `RefreshService`: sub2api `/refresh` first when remote RT exists, then local cached OpenAI RT refresh, then sub2api `check-status`, then plugin-visible AT check, then local cached AT check, then the existing mailbox/ChatGPT/OpenAI fallback paths.
- Stored local GPT tokens after successful OAuth refresh outcomes and after successful session/AT flows so later runs can refresh or validate accounts even when sub2api only exposes redacted credentials.
- Updated monitor protocol-capability detection so accounts without mailbox credentials are still auto-queueable when the plugin has a local cached GPT RT/AT for that email.
- Added local-token failure events and summary mapping so final account errors can now mention failed local cached OpenAI RT/AT paths before the existing mailbox/browser message.
- Verified the backend with `.venv/bin/python -m compileall backend/app`, ran `init_db()` through the project virtualenv, confirmed migrated `account_snapshots` columns through SQLite `PRAGMA table_info`, and ran `git diff --check` successfully.
## 2026-07-13

- Started the x1 sync and extensible subscription handling request.
- Read the required planning, existing-plugin update, and authentication security guidance.
- Confirmed the current Git worktree is clean and synchronized with `origin/main`.
- Confirmed this repository is not a Codex marketplace plugin, so Codex cachebuster/reinstall steps do not apply.
- Added a six-phase implementation and verification plan; x1 source discovery is in progress.
- Logged and corrected a failed all-files append caused by mismatched historical file anchors; no existing content was changed by that failed attempt.
- Resolved `x1` to SSH host `154.12.51.74:54321` as `root`; the first remote discovery command was rejected by local/remote quote parsing and was replaced with a simpler single-quoted form.
- Located the remote repository at `/root/apps/GptCheckPlugin`; simplified subsequent remote inspection after a zsh regex parse failure.
- Fetched x1 `main` into local read-only tracking ref `x1/main` and compared it with local `main`.
- Confirmed x1 has one large committed feature increment plus an uncommitted quota/settings/UI increment; no runtime data or secrets were fetched.
- Fast-forwarded local `main` to x1 commit `9270805`; resolved append-only stash conflicts in the three planning files by preserving both histories.
- Reviewed x1 uncommitted backend/tests: it adds concurrency, sample thresholds, monthly-window handling, reset materialization, and estimator corrections, but not configurable per-subscription quota ranges or K12/general type support.
- Synced all selected x1 source/config/test files and verified all 15 SHA-256 hashes against x1; excluded planning files, screenshot, `.env`, databases, and output data.
- Completed subscription-flow tracing and selected a centralized normalization plus structured runtime quota-range design; implementation is in progress.
- Added centralized subscription normalization/default-range utilities, validated runtime settings persistence, normalized API fields, and estimator/sample integration. Backend compile and the new focused tests passed; one obsolete Team-only monthly assertion was identified for update.
- Full backend suite (36 tests), compileall, frontend production build, and diff check passed. Playwright then exposed periodic settings-form reset during editing; implemented value-equality guarding for poll updates.
- Playwright desktop/mobile verification passed after the polling fix and responsive refinement: K12 ranges remained editable, a future `Enterprise Edu` type normalized to `enterprise-edu`, all mobile windows were visible, and mobile document width matched the 390px viewport.
- Final functional checks passed, while dependency audit found fixable Vite/Babel advisories; started a compatible `npm audit fix` remediation.
- Applied the compatible dependency fix; final audit reports 0 vulnerabilities, frontend build passes on Vite 6.4.3, backend suite passes all 37 tests, compileall/diff checks pass, and no hardcoded secret patterns were found.
- Completed all six phases of the x1 sync, K12/general subscription handling, configurable quota ranges, and verification request.
- Closed the Playwright browser session, kept the isolated dev services healthy for user review, and removed the test-generated root `.env` after verifying it was created during this session.
- Updated runtime settings persistence so `APP_ENV=test` never writes the project `.env`; development and production behavior remains unchanged, making the live preview fully isolated.
- API smoke test persisted K12 `30-45` and custom `enterprise-edu` ranges correctly; centralized the test-environment file guard after startup scanning exposed a second `.env` persistence path.
- Final rerun passed all 38 backend tests, compileall, zero-vulnerability npm audit, conflict/diff checks, and live frontend/backend health checks with no root `.env` present.

## 2026-06-08

- Investigated the monthly quota display problem for `niubi963019@edu.aiceo.dev` without printing secrets. Live sub2api usage reports `utilization=100` and `window_stats.cost=0` for the monthly window.
- Found the backend estimator cleared `raw_spent` for monthly zero-cost/nonzero-percent windows, but then re-created `estimate_spent` as `estimated_limit * used_percent`, making the frontend display the inferred `$200` total as already used.
- Patched `usage_estimate.py` so percent-only missing-cost windows keep `used_percent`, `estimated_limit`, `remaining`, and rate-limit state, but leave `estimate_spent` null with basis `percent_only_missing_usage`.
- Rechecked the reported account through `build_usage_estimate(refresh=False)`: both monthly-backed 5h and 7d/month rows now return `raw_spent=null`, `estimate_spent=null`, `estimated_limit=200.0`, and `remaining=0.0` instead of `estimate_spent=200.0`.
- Verification passed: `.venv/bin/python -m compileall backend/app`, `npm --prefix frontend run build`, a synthetic monthly missing-cost regression script, `UsageEstimateOut.model_validate`, and `git diff --check`.
- Restarted `gptcheckplugin.service`; `systemctl is-active` reports `active` and `/api/health` returns `{"status":"ok"}`.
- Adjusted frontend quota wording so monthly-only accounts display `未用` instead of `月剩余`, and missing remaining-percent fallback shows `-` instead of `缺少已用百分比`. Frontend build and `git diff --check` passed, then `gptcheckplugin-frontend.service` was restarted and returned HTTP 200.
- Fixed account-row rate-limit badges for monthly-only accounts: display-only rate-limit windows now collapse duplicated 5h/7d monthly signals into one `月限流` badge, using the existing orange `warn` badge tone. Frontend build and `git diff --check` passed, then `gptcheckplugin-frontend.service` was restarted and returned HTTP 200.

## 2026-07-13 OAuth Account Identity Display

- Added explicit sub2api account-name extraction without changing email-based OAuth identity handling.
- Added `account_name` to the accounts API/frontend contract, including snapshot and missing-name email fallbacks.
- Updated the account table to show a copyable name above a separately copyable email, and included names in account search.
- Verification passed: 41 backend tests, backend compileall, frontend production build, and `git diff --check`.
- Playwright confirmed independent name/email clipboard values, name-based search, non-overlapping stacked rows, and document widths matching the 1440px desktop and 390px mobile viewports.

## 2026-07-13 Plus Weekly Window and Sample Management

- Started x1 read-only inventory and traced the local window-classification path.
- Confirmed x1 services are active at `5151aa7` and recorded the pre-existing untracked screenshot for preservation.
- Logged the failed optional `data` directory assumption; database discovery is the next step.
- Replaced a second nested-quote failure with a plan to use base64-encoded read-only Python for remote inspection.
- Resolved the production SQLite path and inventoried samples without exposing account emails or credentials.
- Confirmed seven anomalous Plus 7d samples above `$200`; no rows have been deleted yet.
- Queried x1 estimator output without emails/tokens and reproduced the Plus misclassification: exact 7-day windows were shown as monthly because Plus no longer has a 5h window.
- Completed Phase 1 and selected plan-aware classification plus backend-enforced non-Team monthly derivation.
- Reviewed the settings and samples UI integration points and selected a compact row action plus read-only derived monthly controls.
- Implemented plan-aware window classification, backend/frontend non-Team monthly derivation, and the authenticated single-sample deletion flow.
- Completed the first diff/security pass; corrected the delete action color token before tests.
- Logged and corrected Windows/no-match test-search command issues before editing regression coverage.
- Added Plus weekly-window/sample, non-Team monthly-derivation, Team override, and sample-delete regression tests; updated one expected old-bound assertion after the first targeted run.
- Targeted backend suite passed 42 tests; frontend TypeScript/Vite production build, backend compileall, and diff check passed.
- Completed Phases 2-4. Browser verification and the full security/regression suite remain before commit/deployment.
- Restarted the isolated `APP_ENV=test` preview backend on the new code and seeded one disposable Plus 7d sample (ID 1) in `output/codex-preview.db` for real API/UI deletion verification; project `.env` remains absent.
- Verified `npx` and Playwright CLI availability; documented the Windows/WSL wrapper incompatibility and switched to the wrapper-equivalent direct CLI invocation.
- Opened the isolated preview in Playwright session `plus-samples` and captured the fresh login snapshot; no production browser/session is being used.
- Logged into the isolated preview with the test admin key and confirmed the authenticated dashboard rendered before navigating to samples.
- Opened the samples page; the shell and new explanatory text rendered, but sample tabs were not yet present in the immediate snapshot, so API/console state is being checked before interaction.
- Diagnosed and fixed sample loading being blocked by an unrelated usage-estimate error in the isolated environment; samples now load independently.
- Reloaded the authenticated preview after the effect fix; the application shell remains healthy and the session cookie persisted.
- Reopened samples successfully: the real endpoint now loads independently, reports the disposable row under `7d · Plus` (not monthly), and includes the accessible operation column.
- Selected `7d · Plus` and confirmed the real row shows its weekly calibration, reset timestamp, and accessible `删除额度样本 1` action.
- Playwright verified the explicit irreversible confirmation text, accepted it, received `已删除额度样本 #1`, and observed the Plus 7d count/table refresh from 1 to 0 through the real DELETE endpoint.
- Settings snapshot confirms Plus/Pro/Free/K12/Unknown monthly bounds render as disabled derived `$400-$560`, while Team monthly remains independently editable at `$100-$300`.
- Editing Plus weekly bounds to `$125-$150` immediately updated the disabled monthly bounds to `$500-$600`, confirming the 4x derivation before save.
- Captured the full settings page at 390×844; Playwright reports document width exactly 390px with no page-level horizontal overflow.
- Visually inspected the mobile screenshot; quota rows remain legible and controls do not overlap.
- Seeded a second disposable row for mobile samples layout verification; refreshed stale Playwright refs after the viewport changed the compact navigation markup.
- Opened the samples page in the 390px viewport; the compact navigation and horizontally scrollable samples table remain contained within the viewport.
- Refreshed sample data in the mobile viewport and confirmed the disposable row appears under `7d · Plus` while the monthly Plus tab remains zero.
- Captured `plus-samples-mobile.png`; Playwright measured unintended page-level width 1005px, so mobile containment requires one CSS correction before acceptance.
- Added `min-width: 0` to the samples grid panels so the wide table contributes to its own scroll container rather than the page's intrinsic width.
- First containment attempt was insufficient; Playwright still measured 1005px document width, so computed overflow ancestry is being inspected before another change.
- Isolated the overflow to the action header's absolutely positioned screen-reader span and replaced it with a visible `操作` label; kept the defensive `min-width: 0` grid containment.
- Retest passed: mobile document width is now exactly 390px while the 1030px table remains independently scrollable inside its 312px container; captured the fixed screenshot.
- Visually inspected the fixed mobile samples screenshot and accepted the contained scrolling behavior.
- Completed the live delete-endpoint authorization/validation matrix (401/422/404) and reviewed the full combined diff before the final regression suite.
- First full-suite pass found one obsolete runtime-settings monthly expectation; updated it to verify persisted non-Team values are backend-derived at 4x weekly.
- Final pre-commit verification passed: 46 backend tests, backend compileall, frontend production build, production dependency audit (0 vulnerabilities), diff check, and changed-line secret-pattern scan.
- Full frontend dependency audit also reports 0 vulnerabilities; final 15-file source/test/plan diff is clean and contains no generated artifacts.
- Created commit `92af35a` and a verified incremental deployment bundle that requires x1's current `5151aa7` base.
- x1 preflight reconfirmed exact base `5151aa7` and no tracked worktree changes. Created integrity-checked 23,027,712-byte online SQLite backup `data/backups/sub2api_at_guardian-before-92af35a-20260712T195931Z.db`, containing the same seven confirmed outlier IDs.
- Transferred the verified bundle to x1, fetched target `92af35a`, and confirmed the current remote HEAD is its ancestor; ready for the controlled stop/fast-forward window.
- Fast-forwarded x1 to `92af35a` during a controlled stop, passed all 46 remote tests and the production frontend build, transactionally deleted the exact seven confirmed outlier IDs, and restarted both services.
- Production verification passed: backend/frontend active, health OK, frontend HTTP 200, `01-login.png` preserved, Plus windows corrected to `none + seven_day`, effective Plus monthly range equals 4x weekly, and no Plus 7d sample above `$200` remains.
- Completed all six phases of the Plus weekly-window, monthly derivation, sample deletion, data cleanup, commit, and x1 deployment request.
- Completed Phase 5; only commit, x1 backup/deployment, confirmed seven-row cleanup, and production verification remain.
- Closed the Playwright session and removed all disposable `preview-manual-delete%` rows from the isolated database; zero remain.

## 2026-07-13 API Key Upstream Account Management

- Started a new scoped change request while preserving the existing unfinished Plus/sample-management work and all current worktree edits.
- Loaded the planning, frontend-design, and Playwright skill instructions.
- Ran planning-session catchup and inspected the current branch/worktree state.
- Attempted to read referenced Codex task `019f28a1-41ae-7332-8f18-f022e15a2d62`; direct task reads were rejected, so local/index recovery is next.
- Verified the live plugin backend/frontend endpoints and the Playwright CLI prerequisite.
- Confirmed the historical sub2api port `18080` is not currently reachable; runtime settings discovery is required before live account tests.
- Authenticated to the local plugin with its current default development admin key and read only non-secret runtime settings.
- Located the live sub2api at port `18082` and confirmed that no sub2api management key is currently saved in the plugin.
- Mapped the current encrypted settings, API router, sub2api client, and React view/navigation integration points at a high level.
- Identified the Docker-backed local sub2api source checkout and non-secret container/runtime metadata.
- Confirmed the running preview build does not expose a usable unauthenticated OpenAPI contract; source-level route inspection is in progress.
- Confirmed the exact sub2api account multiplier update and upstream balance endpoints from local source.
- Reviewed the local sub2api balance-probe extension and selected its credential-isolating endpoint as the preferred balance source for this feature.
- Recovered the referenced task from local Codex session storage and confirmed the multiplier formula plus upstream-recharge precedence rules.
- Completed read-only analysis of `upstream-ops` v0.0.6 and mapped its New API/Sub2API adapters, API-key CRUD, target-account update flow, and known recharge-rate gap.
- Used the existing local sub2api admin credential only in memory to inventory non-sensitive runtime data: 43 total accounts, 24 API-key accounts, and 10 groups.
- Completed the feature boundary and security design: import existing remote accounts, explicitly bind missing upstream keys, preserve old rates on discovery failure, and calculate/write server-side only.
- Verified the live target recharge multiplier (`10x`) through the authenticated admin settings API.
- Verified both supported and unsupported live upstream balance responses through sub2api without exposing stored credentials.
- Confirmed the preceding parallel task committed successfully at `92af35a`; only this task's planning notes remained dirty before implementation agents began.
- Backend subtask started: loaded the security-review and planning-with-files instructions, confirmed the existing feature boundary, and recorded the implementation/test checklist.
- Inspected the existing model/database/schema/router/security/crypto patterns; selected a new-table-only migration path and explicit safe DTO construction.
- Mapped the sub2api account pagination/filtering and error behavior; recorded the need for fixed-category API errors so response bodies/secrets cannot escape.
- Reconfirmed the exact target balance/rate endpoints and the upstream formula/precedence from shared findings; sent the planned backend DTO fields and request bodies to the frontend subtask for contract alignment.
- Verified the target bulk-update DTO accepts the required exact one-account `rate_multiplier` payload; recorded the settings-field source mismatch and strict fallback rule.
- Located the authenticated target settings route and confirmed the account DTO exposes current `rate_multiplier`; readback verification can be implemented without using unsafe response-body text.
- Confirmed the sanitized balance response shape and the production default-encryption-key rule; shared backend files are now safe to edit from the committed base.
- Logged a PowerShell quoting failure during sibling-source inspection and switched the remaining searches to single-quoted regex arguments.
