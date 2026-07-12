# Findings

## Workspace

- `C:\Users\zanez\Documents\agents_playground\GptCheckPlugin` started empty.
- The directory is not currently a git repository.
- Python 3.14.3, Node v24.14.0, and npm 11.9.0 are available.

## sub2api API

- Public sub2api frontend API references show account management under `/api/v1/admin/accounts`.
- Account records include fields such as `id`, `platform`, `type`, `credentials`, `status`, and `schedulable`.
- Update is exposed as `PUT /admin/accounts/{id}` under the API client base.
- Additional account operations include clear-error and recover-state endpoints.

## Requirement Notes

- GPT account email can be treated as the stable account identifier.
- Error accounts should be refreshed once and then marked/skipped if deactivated.
- The plugin must avoid crashing on mailbox refresh failures and should persist failure reasons.
- Runtime sub2api settings should override `.env` without a service restart for ordinary port/key/interval changes.
- Localhost sub2api probing should ignore system proxy environment variables; otherwise `httpx` can return proxy 502s for `127.0.0.1`.
- Local ports can return generic 200/401 responses for arbitrary paths; sub2api auto-scan must validate the `/admin/accounts` response body shape, not only status code.
- Display time zone is a presentation setting only; stored timestamps remain unchanged and are formatted in the browser with `Intl.DateTimeFormat`.
- SQLite strips timezone info from stored UTC datetimes in API responses, so frontend parsing must append UTC semantics for offset-less ISO strings.
- ChatGPT `/auth/login` can require two email submissions in headless Chromium: the first click may only hydrate the route to `?email=...`, while the second reaches `https://auth.openai.com/email-verification`.
- ChatGPT may reuse a recently sent login code; strict `received_at >= requested_at` mailbox filtering can miss a still-valid code, so refresh uses a bounded lookup grace window.
- sub2api regular `PUT /admin/accounts/:id` preserves omitted sensitive credential keys but replaces omitted non-sensitive credential keys. For minimal credential writes, use single-account `POST /admin/accounts/bulk-update` with a `credentials` patch.
- sub2api `recover-state` can leave `schedulable=false`; successful external refresh should also call `POST /admin/accounts/:id/schedulable` with `true` and clear temp unschedulable state.
- sub2api exposes `GET /api/v1/admin/accounts/:id/usage?source=active&force=true`; for OpenAI OAuth accounts this performs an active quota/usage query and syncs the result into passive usage cache.
- sub2api active OpenAI usage/test paths can persist a 429 response `plan_type` such as `free` into account credentials. After an external browser refresh, treat ChatGPT session `account.planType` as authoritative and re-apply it after usage refresh.
- sub2api account `error_message` can retain stale `account_deactivated` text after an account later proves usable. Do not classify list snapshots as deactivated by scanning the whole account payload; only explicit deactive status/flags or direct ChatGPT/browser checks should drive local deactive state.
- Post-refresh usage refresh should be best-effort: credentials replacement is the critical step, while usage refresh failure should be visible as an event without marking the login refresh job failed.
- Usage-refresh failure details may include upstream text, so event details should redact common token/password/authorization patterns before persistence.
- sub2api admin account list/detail responses redact sensitive credential values including access tokens, so the plugin cannot reliably perform AT checks from the admin list payload alone.
- A server-side sub2api `POST /api/v1/admin/accounts/:id/check-status` endpoint can safely use the stored access token and return token-free fields such as `token_valid`, `deactive`, `plan_type`, email, account id, subscription expiry, and changed credential names.
- The AT-first refresh path should treat upstream status-check timeout/500 as a soft failure and continue to the previous browser/email login path.
- Opening the plugin accounts page should not force an active usage query by itself; cached usage display is enough until the manual button or automatic refresh setting triggers the active usage endpoint.
- Manual all-account usage refresh should call sub2api's forced usage endpoint once, then refresh the UI from cached/account fields instead of issuing a second forced estimate query.
- HAR files are not a good refresh primitive for this app: they can contain cookies, authorization headers, access tokens, refresh tokens, and session payloads, so they should not be stored, parsed, or logged by the plugin.
- sub2api already exposes lightweight server-side OAuth refresh for stored OpenAI OAuth accounts at `POST /api/v1/admin/accounts/:id/refresh` and `POST /api/v1/admin/openai/accounts/:id/refresh`.
- The `/admin/accounts/:id/refresh` response is built through DTO credential redaction, so the plugin can trigger refresh without receiving raw `access_token` or `refresh_token`.
- Lower-memory refresh order should be: sub2api server-side access-token status check, sub2api server-side OAuth `refresh_token` refresh only when `credentials_status.has_refresh_token=true`, plugin-visible access-token check if one exists, then Playwright browser/email login only as the last fallback.
- Protocol smoke refresh for sub2api account `3` completed without Playwright and recorded `78,909,440` bytes peak RSS (about `75.3 MiB`), but follow-up inspection showed account `3` has `has_access_token=true` and no `has_refresh_token`. That smoke test exercised sub2api's `/refresh` fallback that reuses an existing access token, not a true RT refresh.
- For no-RT accounts, ChatGPT Web's own NextAuth flow can be driven without Playwright: `GET /auth/login` for CSRF, `POST /api/auth/signin/openai`, follow the returned `auth.openai.com` continuation, submit email to `/api/accounts/authorize/continue`, validate OTP through `/api/accounts/email-otp/validate`, follow the returned ChatGPT callback URL, then read `/api/auth/session`.
- The Codex CLI OAuth client flow can land on an `add_phone` page for `JadeSanchez2515@outlook.com`, but the ChatGPT Web NextAuth flow for the same mailbox returned an `external_url` callback and produced a session successfully.
- Direct service smoke test of the new ChatGPT Web protocol refresher for `JadeSanchez2515@outlook.com` returned a session/access token without printing secrets and peaked at `90,566,656` bytes RSS (about `86.4 MiB`).

## Deactivated Account Handling Request

- Reported behavior: `EdenBeard6250@outlook.com` had a detected error; manual/system refresh can refresh, but a later connection test reports the account as deactive even though the mailbox/account is still loginable.
- Imported known deactive account: `NiaLumsden2003@outlook.com`; the program began detection after sub2api import without first confirming that the sub2api account record exposed a mailbox/email.
- Requirement: account identity for detection must come from the email field under the sub2api account record, not from the account name, because names are not guaranteed to be emails.
- Requirement: if an account is deactive, classify it under disabled/deactivated status and provide one-click deletion of deactivated accounts from local accounts, associated mailboxes, and sub2api.
- Local DB snapshot after the report: `nialumsden2003@outlook.com` is `status=error`, `schedulable=false`, `deactive=false`; latest refresh job 42 failed with `Session endpoint did not include accessToken.` rather than being classified as deactive.
- Local DB snapshot after the report: `edenbeard6250@outlook.com` is `status=active`, `schedulable=true`, `deactive=false`; recent jobs 36-40 succeeded, so later deactive detection likely comes from a sub2api test-connection/usage response path not currently mapped to local deactive state.
- Current monitor already skips accounts when `Sub2ApiClient.account_email(account)` returns no email, but the helper currently searches the whole account object and can fall back to `name`; this violates the requirement to use the email field under the account record rather than account name.
- sub2api live logs show account `4` (`EdenBeard6250`) test requests returning HTTP 401 with code `account_deactivated` at `2026-05-24T13:20:58Z`, `13:22:23Z`, `13:24:59Z`, `13:26:17Z`, `13:27:03Z`, and `13:40:49Z`. The local plugin later synced it as active because sub2api's account list currently reports `status=active` and empty `error_message`.
- sub2api live logs show account `5` (`NiaLumsden2003`) imported at `2026-05-24T13:34:02Z`; usage/test requests returned `token_invalidated`, not `account_deactivated`, and the plugin queued a browser refresh once a mailbox row existed.
- sub2api exposes account deletion as `DELETE /api/v1/admin/accounts/:id`; the plugin can use the configured accounts path plus `/{id}` for remote deletion.
- sub2api account test is SSE over `POST /api/v1/admin/accounts/:id/test`; account `4` returns a `type=error` event whose payload contains code `account_deactivated`. This can be parsed by reading the stream text, without needing sub2api to persist the test error into the account list.
- Implemented account email extraction as an explicit allowlist of email/profile fields (`credentials.email`, `extra.email`, etc.) and no longer searches `name`, `notes`, or arbitrary raw account text.
- After the backend restart, the monitor refreshed/deactivated `NiaLumsden2003@outlook.com`: browser flow detected `account_deactive` after code validation. A cleanup endpoint smoke test then deleted that deactivated local snapshot, its mailbox row, and sub2api account `5`.
- After authenticated sync, `EdenBeard6250@outlook.com` is now locally classified as deactive because sub2api account `4` carries an `account_deactivated` error message; sync counted `total_seen=3`, `error_seen=1`, `queued=0`.
- Recovery note: `NiaLumsden2003@outlook.com` was recoverable because sub2api account `5` was soft-deleted in Postgres (`deleted_at` set) and the local mailbox row remained intact in SQLite free page 42.
- The recovered local mailbox row for Nia had row id `4` and four complete encrypted Fernet fields; no raw mailbox secrets were printed or needed.
- `Sub2ApiClient.is_deactive_account()` must treat `error_message` deactivation text as authoritative only when the account is in an error/failed or unschedulable state. This keeps Eden classified as deactive while avoiding stale error text on active accounts.

## 2026-05-24 Incident: sub2api Service and Mailboxes

- Local port check shows `127.0.0.1:18080` is listening and `/api/v1/admin/accounts` returns HTTP 401 without credentials, so sub2api is reachable at the socket/API layer.
- Local plugin backend port `127.0.0.1:8000` is listening, but `/health` returns 404; verify the correct health path from the app before treating this as service failure.
- The project root currently has no `.env` file. If existing mailbox rows were encrypted with a previous `APP_ENCRYPTION_KEY`, starting the backend without that key can make rows unreadable even when the SQLite data still exists.
- Root cause found: the running backend was started with a working directory that caused relative SQLite URL `./data/sub2api_at_guardian.db` to resolve to `backend/data/sub2api_at_guardian.db`, an empty newly created DB. The recovered DB with 4 mailboxes/4 accounts remained at project-root `data/sub2api_at_guardian.db`.
- Fix applied: SQLite relative database paths are now resolved against `settings.project_root`, so starting uvicorn from either project root or `backend` uses the same restored DB.
- After restart, authenticated API reads show 4 accounts and 4 mailboxes, `sub2api_x_api_key_set=true`, and account sync returns `total_seen=4`, `error_seen=1`, `queued=0`.
- Mailbox API smoke checks: Nia, Jade, and Eden returned successfully without exposing message contents; Micah/Hotmail timed out after the app's 45-second mailbox read timeout.
- Follow-up panel issue: `http://127.0.0.1:18080/`, `/admin`, `/dashboard`, and `/login` all return 404 because the local sub2api binary is serving API routes only. The Vue panel must be run from `sub2api/frontend` on port `3000` with `VITE_DEV_PROXY_TARGET=http://127.0.0.1:18080`.
- Started the sub2api frontend on `http://127.0.0.1:3000`; verified the public settings API proxies through Vite to `18080` and logged in to the dashboard with the local admin credentials.
- Updated sibling project scripts `C:\Users\zanez\Documents\agent_playground\sub2api\start-local.ps1` and `stop-local.ps1` so future local starts/stops include the frontend panel. That sibling directory is not a git repository.

## 2026-05-25 Memory Guard and Split Concurrency

- `RefreshService` currently samples RSS for the backend process tree, so Playwright/Chromium child processes are included in `memory_peak_rss_bytes`; protocol-only refreshes should stay much lower than browser fallbacks.
- The existing `refresh_max_concurrency` controls all refresh jobs together. To avoid browser fallback spikes, protocol work and Playwright browser login need separate limiters.
- The browser guard should check available system memory, not current app RSS: if available memory is below 500 MiB by default, the browser fallback should fail fast and leave protocol/sub2api paths unaffected.
- In containerized Linux, host `/proc/meminfo` can overstate available memory. The guard now takes the smaller value between procfs/psutil available memory and cgroup memory headroom when cgroup limits are present.
- Existing persisted `refresh_max_concurrency` remains a compatibility alias for protocol concurrency; browser fallback uses its own `browser_refresh_max_concurrency` key.

## 2026-05-25 Recovery Toggle and File-backed Settings

- The recovery behavior is sub2api post-update recovery: clear error, call `recover-state`, set `schedulable=true`, and delete temporary unschedulable state.
- The broader account recovery task is the plugin refresh/repair queue. Disabling it should prevent automatic monitor enqueueing and reject manual refresh requests.
- The initial implementation defaulted `recovery_enabled` to true when no DB/config value existed, so a missing setting row meant manual sync still queued recovery. The default must be false to match an unchecked settings toggle.
- Runtime settings were stored in SQLite `app_settings`; if the database is recreated or a different working directory is used on redeploy, admin-panel settings can appear reset.
- Pydantic previously read `.env` relative to the process working directory. Loading it from the project root avoids a backend-start-directory mismatch.
- Admin-panel settings now write to project-root `.env`, preserving existing unrelated lines and writing x-api key only to the ignored config file.

## 2026-05-25 Bulk Delete Problem Accounts Button

- The top-right accounts button was disabled because it counted only `deactive` accounts. The current sub2api state has many GPT accounts in `status=error`, but none with local `deactive=true`.
- User clarified ordinary remote `status=error` accounts may recover after reauthorization and must not be directly deleted.
- The intended bulk cleanup target is a duplicate email group with a usable account plus an unusable duplicate. Delete only the abnormal duplicate when the group has a non-error, non-deactivated primary replacement.
- `remote_error` is still useful for display, but it must not by itself imply `can_delete_remote` or bulk deletion eligibility.

## 2026-05-25 OAuth RT Acquisition

- Current plugin refresh already has a low-memory ChatGPT Web protocol login path, but it fetches `/api/auth/session` and usually only yields an access token, not an OAuth refresh token.
- sub2api account detection already recognizes `credentials.refresh_token`, `credentials.refreshToken`, and `credentials.rt`; writeback currently standardizes on `refresh_token` when a session exposes a refresh token.
- To obtain durable GPT RTs, the plugin should drive OpenAI OAuth with PKCE, capture the callback `code` without exposing tokens, exchange it server-side, then write the resulting `access_token`, `refresh_token`, and `id_token` into sub2api credentials.
- A protocol implementation can likely reuse the existing `auth.openai.com/api/accounts/authorize/continue` and `email-otp/validate` requests. Browser automation remains useful as a fallback when the protocol flow encounters unsupported page states or Cloudflare.
- Protocol OAuth smoke test for `annamason5243@outlook.com` reached `sign_in_with_chatgpt_codex_consent` after email OTP, then stopped because the consent page is a JS-rendered React/Remix page rather than a simple form/API continuation. Keep pure protocol OAuth disabled by default until that consent action is mapped.
- Browser OAuth smoke test for the same account succeeded after adding request/frame callback capture. Chromium navigated to the local callback and then `chromewebdata` because no listener exists on port 1455, so the code must be captured before the error page replaces the URL.
- Successful browser OAuth smoke test wrote back `access_token`, `refresh_token`, `id_token`, `email`, and expiry through the existing sub2api credentials patch path without printing secrets. Peak process-tree RSS was about `834.9 MiB`.
- Existing RT refresh for `annamason5243@outlook.com` through sub2api remained low-memory, peaking around `77.4 MiB`.
- Refresh ordering should be RT-first when a refresh token exists: sub2api `/refresh` now runs before AT status check so accounts with RT actively rotate/refresh through the intended token path instead of stopping early on a still-valid AT.

## 2026-05-25 Usage Estimate Availability

- The sub2api usage endpoint succeeded for all 42 deduped GPT accounts during diagnosis and returned `cost` for every account, so the main issue was not upstream fetch failure.
- Many accounts were marked not estimable because the estimator used `raw_spent - baseline_spent` as the numerator. On first enable or after no new usage, this delta is zero, even though official current-window raw usage and official used percent are enough to estimate remaining quota.
- The correct remaining-quota estimate should use the current official window raw usage divided by official used percent. The baseline remains useful for showing "new usage since tracking started", but should not gate the remaining-quota estimate.
- The accounts page initially calls the usage estimate API with `refresh=false`; without cached raw cost from the last usage refresh, it can only see percentages from account fields and cannot estimate. Cached `UsageWindowState.last_raw_spent` and matching cached percent should be used for that lightweight view.

## 2026-05-27 Calibrated Usage Quota Display

- The robust place to update quota-limit samples is every path that obtains active usage-window data: quota estimate refresh, manual/automatic usage-window refresh, and post-account-refresh usage refresh.
- A conservative sample trigger is needed so ordinary mid-window observations do not pollute the limit table. The implementation records samples when official usage percent is at least `99%` or when usage/window/account status text carries rate-limit/quota-exhaustion markers.
- Before enough samples exist, default clamp ranges should prevent obviously inflated reverse limits: 5h uses `$15-$25`; 7d uses `$100-$140`.

## 2026-05-27 Usage Estimate History

- Superseded: the requested history is not aggregate estimate history. The UI should show the actual 5h/7d limit samples used to calibrate the official-window quota estimate.
- The sidebar issue comes from the whole shell scrolling together. Desktop layout should lock the sidebar to viewport height and make only `.workspace` scroll.

## 2026-05-27 Account Rate-Limit Distinction

- The account table previously treated active/schedulable accounts as simply available even when their 5h/7d usage window had reached the practical limit.
- Existing usage-estimate/sample logic already identifies rate-limited windows via official used percent >= `99%` or rate-limit/quota markers in usage/account payloads, so the UI distinction should reuse that signal instead of adding a separate probe.
- The accounts page can combine backend account-level rate-limit flags with cached usage-estimate window flags, because opening the accounts page intentionally reads cached usage data without forcing an active upstream query.

## 2026-05-28 Refresh Restart Loop

- The backend service had entered a systemd restart loop with thousands of restarts. The immediate repeated startup failure was `address already in use` for `127.0.0.1:8000` because an orphaned uvicorn process from the project was still listening outside the active systemd control flow.
- The earlier trigger was `sqlite3.OperationalError: database is locked` while refresh background tasks were writing optional `refresh_memory_peak` events. That exception escaped during asyncio shutdown and caused the backend process to exit.
- Uvicorn runs FastAPI lifespan startup before binding the listening socket. When the port was already occupied, the monitor loop could still run, sync sub2api, enqueue refresh jobs, then the process would fail binding and restart. This produced repeated `Service restarted before refresh finished` jobs.
- After the service was stabilized, the latest real refresh attempts did not fail from restart anymore. They completed as `deactive` because OpenAI OAuth OTP validation returned `account_deactive` for the affected accounts.
- The sub2api stored-token refresh path returned Cloudflare HTTP 502 from `https://ai.duckduckport.top/api/v1/admin/accounts/:id/refresh`, so refresh jobs correctly fell back to the mailbox/OAuth path before detecting deactivation.

## 2026-06-05 Local OpenAI RT/AT Refresh Port

- `mail-console-x1` contains a direct OpenAI token refresh implementation that posts `grant_type=refresh_token` to `https://auth.openai.com/oauth/token`, then immediately validates the returned access token through ChatGPT `accounts/check` before updating local state.
- The current plugin previously had OAuth code exchange and ChatGPT access-token checks, but it did not have a local cached GPT refresh-token store or a direct OpenAI `refresh_token` refresh path.
- sub2api admin account payloads are redacted, so a local direct OpenAI RT refresh path is only feasible if the plugin securely caches GPT OAuth tokens from prior successful OAuth/browser refresh outcomes.
- Minimal port strategy: store encrypted GPT `refresh_token` / `access_token` / `id_token` / `client_id` plus expiry on `account_snapshots`, then try local cached OpenAI RT refresh before falling back to sub2api check-status, plugin-visible AT checks, ChatGPT protocol login, and browser OAuth.
- For no-mailbox accounts, monitor auto-queue logic must treat local cached GPT tokens as a protocol-capable recovery path; otherwise those accounts would still be skipped before the new RT/AT flow can run.
## 2026-07-13: x1 Sync and Subscription Extensibility

### Confirmed So Far

- Current repository is clean on `main` at `55f34c7`, matching `origin/main`.
- The repository is an application/browser-plugin-style project with `backend/` and `frontend/`; it is not a Codex plugin and has no `.codex-plugin/plugin.json`.
- The user requires explicit K12 OAuth subscription handling plus forward-compatible type recognition, labels, filters, sample persistence, and settings-managed default quota ranges.
- OAuth credentials and mailbox data are sensitive; implementation and tests must avoid printing access tokens, refresh tokens, authorization headers, cookies, passwords, and OTP codes.

### Open Discovery

- Resolve what host/path/branch the name `x1` maps to in this environment.
- Determine the exact K12 plan string(s) emitted by sub2api/OpenAI and how existing plan values flow through the backend and frontend.
- Determine whether x1 already contains compatible improvements that should be synced before extending the design.

### x1 Source State

- `x1` resolves to SSH host `154.12.51.74:54321` as `root`; the project is `/root/apps/GptCheckPlugin`.
- x1 `main` is at `9270805`, one commit ahead of local `main`/`origin/main` (`55f34c7`).
- Commit `9270805` adds the missing foundation: OAuth/token refresh paths, phone bindings, exception history, subscription refresh, subscription metadata, calibrated usage estimates/samples, and expanded account UI/settings.
- x1 also has 17 modified/untracked worktree files. The code changes are concentrated in runtime configuration, usage estimation/samples, account schemas/routes, refresh services, and frontend types/UI/styles; these are likely the source for configurable quota intervals.
- The x1 worktree is not clean, so fetching x1 Git `main` alone is insufficient. Sync must preserve both the committed commit and selected uncommitted source changes while excluding untracked screenshots, databases, `.env`, and secrets.

### x1 Uncommitted Design Review

- Runtime settings add usage-refresh concurrency and per-window sample trigger percentages; they do not yet expose per-subscription default quota ranges.
- Usage estimation adds monthly-window handling, derived reset timestamps, threshold-driven sample capture, safer calibration clipping, and broader retained-sample calibration.
- The committed cohort model remains centered on `plus`, `team`, and `unknown`, with hardcoded default limits. K12 and arbitrary future subscription values still need a generic normalization/configuration layer.
- x1 tests materially expand estimator coverage and include a new runtime-config test file; these should be synced before local extensibility changes so the local implementation starts from the same tested baseline.

### Extensible Subscription Design

- Root cause: `_normalize_plan_cohort()` recognizes only Plus/Team/Pro/Free and converts every other non-empty plan string to `unknown`; `_usage_limit_sample_allowed()` then rejects `unknown`, so K12/future OAuth plans lose cohort identity and samples.
- Monthly samples are additionally restricted to Team, which would discard a real monthly K12/future plan even when upstream provides a valid monthly window.
- Introduce one canonical subscription type normalizer with explicit aliases for K12 and existing plans, plus a sanitized stable slug fallback for new non-sentinel plan strings.
- API outputs should expose normalized `subscription_type` and `subscription_label` alongside raw `subscription_plan`; frontend labels and dynamic subscription filters should consume these normalized values.
- Persist quota defaults as a validated runtime-settings map keyed by normalized subscription type, with independent `five_hour`, `seven_day`, and `monthly` lower/upper bounds. Include `unknown` as the configurable fallback and allow custom keys so future types do not require a release.
- Sample rows continue using the existing indexed `plan_cohort` column for compatibility, but it stores the canonical subscription type; API responses add clearer subscription aliases without a destructive database migration.

### Verification Outcome

- x1 committed code was fast-forwarded to `9270805`; 15 selected x1 worktree files were copied byte-for-byte and hash-verified before local extension work.
- K12 aliases and future plan strings now normalize centrally; unknown sentinel/missing values retain the explicit `unknown` fallback.
- Per-type 5h/7d/month default ranges are validated, persisted in app settings, exported through `USAGE_LIMIT_DEFAULT_RANGES_JSON`, and consumed by estimator calibration/sample validation.
- Playwright verified K12 range editing, custom `Enterprise Edu` creation as `enterprise-edu`, poll-resistant unsaved edits, and non-overlapping desktop/mobile layouts.
- Security review found no new secret exposure. A compatible audit fix upgraded Vite 6.4.2 to 6.4.3 and patched Babel dependencies; final npm audit is clean.

## 2026-06-08 Monthly Usage Missing Cost Display

- Reported account `niubi963019@edu.aiceo.dev` maps to sub2api account `1056` and is a Team account.
- Live sub2api usage for that account returns monthly-style window data with `utilization=100`, reset in July, and `window_stats.cost=0` for both the reused 5h display path and 7d/month path.
- The plugin already treats monthly `cost=0` plus nonzero percent as missing cost data by clearing `raw_spent`, but it then filled `estimate_spent` from `estimated_limit * used_percent`, which made the UI show `$200` as used even though sub2api has no `$200` cost record.
- Correct behavior: keep official percent, estimated total, remaining, and rate-limit status, but leave spent/estimate_spent null when there is no actual cost record. This avoids presenting an inferred monthly total as real usage.
