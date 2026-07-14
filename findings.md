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

## OAuth Account Identity Display (2026-07-13)

- The accounts API currently exposes only the normalized email, even though sub2api keeps a separate top-level account `name`; the frontend therefore cannot distinguish names from emails.
- Email must remain the stable identity for OAuth refresh, mailbox lookup, deduplication, and deletion. The account name is display/search metadata only.
- Name extraction should use an explicit allowlist of name fields and fall back to the normalized email only when constructing the API response.

## Plus Weekly Window and Sample Management (2026-07-13)

- The current `_window_kind()` promotes a `seven_day` window to `monthly` whenever parsed window minutes exceed `MONTHLY_WINDOW_MINUTES_THRESHOLD`; this can misclassify a weekly Plus entitlement when the upstream reset timestamp is farther away than a normal seven-day duration.
- x1 is currently on commit `5151aa7`; both backend and frontend services are active. Its only visible worktree item is the pre-existing untracked `01-login.png`, which must be preserved during deployment.
- The first remote inventory found no database under `/root/apps/GptCheckPlugin/data`; the configured production database location needs to be resolved from service/environment configuration without printing secret values.
- `/root/apps/GptCheckPlugin/data` does exist, but the earlier file probe was invalidated by zsh glob parsing. The systemd unit has the expected working directory and no separately reported `EnvironmentFiles` entry.
- Production uses SQLite at `/root/apps/GptCheckPlugin/data/sub2api_at_guardian.db`.
- The x1 sample inventory contains 37 Plus `seven_day` rows ranging from `$102.8586` to `$310.7235`; exactly seven rows exceed `$200` (IDs 1696, 1889, 2659, 2679, 2692, 2702, and 2770). These are the confirmed cleanup set, pending backup and post-delete verification.
- Team samples are already separated into monthly and seven-day cohorts. The requested non-Team monthly default rule should not relabel actual Plus windows; it only defines the fallback monthly range if a non-Team monthly window is ever observed.
- x1 currently has two Plus accounts. Both upstream windows report `window_minutes=10080` (exactly seven days) with resets on 2026-07-20, but the estimator returns `five_hour.window_kind=monthly` and `seven_day.window_kind=monthly` for both.
- The direct cause is the plan-agnostic branch in `_estimate_window_values()`: any seven-day window is promoted to monthly when `_account_has_no_independent_five_hour()` is true. The five-hour fallback repeats the same promotion.
- Correct classification needs the normalized subscription type: Plus should retain `seven_day`, its missing 5h slot should remain `none`, while Team can retain the legacy monthly-only inference. Explicit upstream monthly aliases remain authoritative for any plan.
- Current quota defaults use `$100-$140` weekly and `$100-$300` monthly for every plan. To make the non-Team monthly rule stable for future types and setting edits, backend normalization should derive non-Team monthly bounds from `seven_day * 4`; Team keeps an independently configurable monthly range.
- `_usage_window_data()` already identifies explicit monthly aliases separately. Therefore the safe order is: explicit monthly alias wins for every plan; otherwise only Team may infer monthly from a missing 5h or monthly-sized legacy window; non-Team plans retain the upstream seven-day kind.
- Existing Team regression tests cover missing-5h monthly inference and explicit `monthly` payloads, so Plus-focused tests can be added without weakening Team behavior.
- The settings UI currently exposes independent monthly inputs for every plan. It should show non-Team monthly values as derived/read-only and update them immediately when the weekly bounds change; Team retains editable monthly inputs.
- The samples table already has a dense, horizontally scrollable layout. A final compact action column using the existing icon-button pattern is sufficient; no new card or dialog is needed. Deletion should use the existing confirmation pattern and refresh samples only after the authenticated API succeeds.
- `runAction()` reloads the general dashboard but not sample data, so sample deletion needs to explicitly call `loadUsageLimitSamples()` in its action.
- The deletion route is ordered before the generic `/{account_id}` route, uses ORM primary-key lookup, requires `require_admin`, validates `sample_id >= 1`, and returns 404 without mutating when absent.
- Initial delete-button styling referenced a nonexistent `--danger` token; corrected it to the existing theme-safe `--danger-ink` token before visual testing.
- Regression coverage now targets the exact x1 Plus shape (`5h=0`, `7d=10080`), asserts the upstream reset fields survive, and ensures new samples remain in the `seven_day` cohort. Separate tests preserve Team monthly overrides and delete-endpoint commit/404 behavior.
- Playwright exposed an existing coupling: a failed usage-estimate request set `usageError`, and the shared effect returned before loading the independent samples endpoint. The sample load is now evaluated first, so the samples page remains operable even if sub2api usage is temporarily unavailable.
- The browser console errors in the isolated preview are expected initial 401 probes and unavailable fake-sub2api 502 responses; they are not caused by the new deletion API.
- End-to-end deletion behavior is correct: confirmation precedes the request, success notice includes the sample ID, and the samples endpoint is reloaded so the tab count and table update immediately.
- Settings behavior matches the invariant: non-Team monthly fields are read-only and reactively follow weekly edits at exactly 4x; Team's monthly fields remain editable.
- Visual review of `output/playwright/plus-settings-mobile.png` confirms the existing dense operations-console hierarchy is preserved: plan labels, weekly inputs, and derived monthly inputs remain readable at 390px with no overlap or clipped controls.
- Mobile samples verification found a real responsive defect: the inner table wrapper is 312px wide with a 1030px scroll area, but the document itself expands to 1005px. The samples panel/grid needs `min-width: 0` containment so horizontal scrolling stays inside `.table-wrap`.
- Computed ancestry confirmed `.table-wrap` already clips correctly at 312px. The remaining 1005px document width matched the absolutely positioned `.sr-only` span in the far-right action header, which escaped the inner scroll geometry. Replacing it with a visible compact `操作` header keeps semantics and containment.
- Visual review of `plus-samples-mobile-fixed.png` confirms the samples hero, tab matrix, calibration card, and table viewport fit cleanly at 390px; wide columns remain intentionally available through inner horizontal scrolling.
- Live security checks against the isolated backend returned the expected status matrix: unauthenticated delete `401`, invalid ID `0` returns `422`, and authenticated missing ID returns `404` without mutation.
- Final code review confirms the earlier account-name/email work is part of the same uncommitted working tree and will be included in this requested commit/deployment; no generated preview database or screenshots are tracked.
- Production readback after deployment shows both Plus accounts as `five_hour=none`, `seven_day=seven_day`, `window_minutes=10080`, with their upstream 2026-07-20 reset timestamps preserved.
- Effective x1 ranges now read Plus weekly `$100-$140`, Plus monthly `$400-$560`, and Team monthly unchanged at `$100-$300`.
- The seven confirmed Plus 7d rows above `$200` were deleted transactionally after backup. Post-delete Plus 7d data has 30 rows, range `$102.8586-$198.6886`, and zero rows above `$200`.

## 2026-07-13 API Key Upstream Account Management

### Backend subtask notes

- The server-side overlay must list all remote API-key accounts, including unmanaged rows; local persisted state is optional and keyed uniquely by the numeric-looking remote account id.
- Every new route is admin-only. Credentials are encrypted at rest, omitted from public models, never echoed in errors, and preserved when PUT sends blank values unless the explicit access-token clear flag is set.
- The upstream client contract is `discover_upstream(...)` / `UpstreamDiscoveryClient.discover(...)` returning `DiscoveryResult` and `GroupOption`, with base URL, type, API key/access token, New API user, and selected group id/name inputs.
- Discovery remains read-only and applies precedence `auto > manual > default 1`; a default recharge result must have a null discovered value. Apply must re-discover and compare a four-decimal `ROUND_HALF_UP` target against the client-confirmed value before issuing an exact one-account bulk update.
- Existing encryption helpers derive Fernet from `APP_ENCRYPTION_KEY`; `encrypt_text`/`decrypt_text` and `redact` can be reused. Public response schemas will be constructed explicitly, never from `__dict__`, so encrypted columns cannot leak.
- Existing route registration uses one module router mounted in `app.api.__init__`; the new router should be mounted at `/api/upstream-accounts`, with `Depends(require_admin)` on every endpoint.
- `init_db()` already runs `Base.metadata.create_all`; because this feature adds a new table instead of columns to an existing table, no manual SQLite ALTER migration is required.
- `Sub2ApiClient._request` currently embeds up to 500 response-body characters in raised errors, so the new API layer/service must catch it and persist/return only fixed safe error categories. The low-level client behavior is left compatible for existing callers.
- Existing pagination uses `/admin/accounts?page=...&page_size=100`; `list_api_key_accounts` can filter `type/account_type/auth_type` after the complete pagination result. Exact remote IDs are available through `account_id`, whose numeric parser already rejects non-positive/non-numeric values.
- A test search command using `backend/test*.py` failed on Windows wildcard semantics; subsequent searches must use `rg -g 'test*.py'` or direct paths.
- Local source findings confirm the target's account balance endpoint is `/admin/accounts/:id/balance`, account writes accept `rate_multiplier`, and the established single-account patch transport is `POST /admin/accounts/bulk-update` with numeric `account_ids`.
- The remote account list intentionally omits its stored API key but includes non-secret credentials such as `base_url`; unmanaged rows can therefore be displayed and balanced through target sub2api while direct group/recharge discovery waits for explicit local key binding.
- The target bulk update DTO has `AccountIDs []int64` and `RateMultiplier *float64`; the client method must send exactly `{"account_ids":[id],"rate_multiplier":value}` and reject non-positive/non-numeric IDs before transport.
- A search for `payment_balance_recharge_multiplier` in the sibling checkout returned no match; the feature contract still requires reading that field from `/admin/settings`. Parsing will tolerate common API envelopes but will default to 1 only when the authenticated HTTP call succeeds and the field is absent.
- The target account DTO explicitly exposes non-secret `rate_multiplier`, `credentials_status`, and redacted credentials metadata; current-rate reads should use the fresh list/get result rather than assuming the bulk-update response contains the updated row.
- `/api/v1/admin/settings` is a registered authenticated route, so the required local recharge read can share the runtime-configured sub2api authentication and must propagate any auth/HTTP error instead of silently defaulting.
- Target balance success data is a sanitized object with `status`, `message`, optional `remaining`/`total`/`used`/`unit`, and `checked_at`; plugin persistence can map those fields directly after safe numeric coercion.
- Target account DTO guarantees `name`, `platform`, `type`, redacted `credentials`, `credentials_status`, `rate_multiplier`, and `status`, which are sufficient for unmanaged account rows and current-rate readback without any secret access.
- Parent confirmed the shared files are clean at HEAD `92af35a`; production with the default encryption key must reject only attempts to add/replace a secret, while development/test may accept encrypted local secrets for preview/testing.
- Public account output now includes separate group/upstream-recharge/target-local-recharge source and status fields; local recharge source/status are persisted so a successful `/admin/settings` response with a missing field remains visibly distinguishable from an explicit value of 1 on later GETs.
- First compile/diff check passed. A pre-test boundary review caught two type-domain issues: balances may legitimately exceed the 1000 multiplier cap, while account IDs must reject booleans/non-integral floats rather than coercing them; both were corrected before test coverage was added.
- Live target data includes a valid negative remaining balance (`-40.904106`), so balance validation now accepts finite signed values within an absolute safety ceiling. Failed/unsupported balance attempts preserve both prior numeric fields and the prior successful `balance_checked_at`.
- Auto-discovered multiplier sources now persist the adapter's precise source strings (for example `groups.available`, `groups.rates`, or `status.price`); only manual/default fallbacks use generic source labels.
- Changed-line secret/error review found no logging or exception-string persistence in the new service; credentials appear only at encryption/decryption boundaries and the in-memory upstream adapter call. All route-visible service errors are fixed public messages.
- The parallel frontend contract includes `remote_status`, concrete source strings, and `confirmed_target_rate`; it remains compatible with the finalized backend response names.
- Secret length constraints cannot safely live in FastAPI/Pydantic fields because default 422 details include the rejected `input`. API-key/access-token length and conflicting set/clear validation now run in the service and raise fixed safe 422 messages; a real route-response regression test proves the oversized secret is absent.
- Multiplier source columns use 128 characters so full adapter labels such as `payment.checkout-info.balance_recharge_multiplier` are not truncated on databases that enforce VARCHAR lengths.


- The current worktree already contains substantial uncommitted changes from an earlier task; they must be preserved and reviewed for overlap before editing shared account/API/UI files.
- The requested reference task ID is not directly readable through the Codex thread API so far; the recent-task index only returned this current task because the reference ID appears in its prompt.
- Required external behavior: discover upstream group multipliers and balance, combine the selected upstream multiplier with a recharge multiplier, and synchronize the resulting billing multiplier to a corresponding sub2api account.
- The plugin backend is healthy at `http://127.0.0.1:8000/api/health` and the frontend is healthy at `http://127.0.0.1:5173`.
- `127.0.0.1:18080` is currently unreachable even though older project notes used that port; live sub2api discovery must use the persisted runtime settings or a fresh scan.
- `npx` is installed at `C:\Program Files\nodejs\npx.ps1`, satisfying the Playwright CLI prerequisite.
- The authenticated plugin settings endpoint reports the live sub2api base URL as `http://127.0.0.1:18082/api/v1`, discovered automatically; `/admin/accounts` returned the expected unauthenticated 401 during discovery.
- The plugin currently has no persisted sub2api `x-api-key`, so authenticated account reads/writes require locating the already-running local service's management credential without exposing it.
- The existing `Sub2ApiClient` already lists groups from `/admin/groups/all` with `/admin/groups` fallback and creates accounts with a default `rate_multiplier: 1`; this is the likely write field for the requested billing multiplier.
- The app uses one encrypted `AppSetting` store for runtime secrets, an authenticated `/api` router, and a single React `View`/navigation shell, so the new feature can fit existing security and UI patterns without a second settings system.
- Port `18082` is Docker container `sub2api-r2-preview-app`, built from `Wei-Shaw/sub2api`; its source checkout is available at `C:\Users\zanez\Documents\agents_playground\sub2api` and its bound data directory is `sub2api\deploy\data`.
- The container environment exposes no plaintext admin API-key variable. Existing authentication must be obtained through the product's own login/key-management path or an already configured credential; secrets must not be scraped into logs.
- `/openapi.json` returns a non-OpenAPI document and the API-prefixed variants return 404, so local source routes are the authoritative contract for this preview build.
- Local sub2api accepts `rate_multiplier` as a pointer field in `PUT /api/v1/admin/accounts/:id`; values below zero are rejected and omitted fields remain unchanged.
- Local sub2api exposes `GET /api/v1/admin/accounts/:id/balance`, which fetches the account internally and calls `QueryUpstreamBalance`, keeping stored credentials server-side.
- The local sub2api worktree already extends balance probing for OpenAI/Anthropic/Gemini/Antigravity/Grok API-key accounts, tries New API-compatible usage/balance endpoints, normalizes quota points at `500000` points per USD, and returns sanitized statuses/messages.
- Admin sub2api requests support either `x-api-key` or an administrator JWT. Reusing the server-side balance endpoint avoids storing or transmitting an additional copy of each upstream API key in this plugin.
- The sibling sub2api checkout is dirty with unrelated user work; it is reference/runtime scope only and must not be modified by this task.
- The referenced Codex task was recovered from `C:\Users\zanez\.codex\sessions\2026\07\03\rollout-2026-07-03T23-38-11-019f28a1-41ae-7332-8f18-f022e15a2d62.jsonl` after thread APIs rejected the ID.
- The referenced task originally used `upstream_group_rate * local_balance_recharge_multiplier / upstream_balance_recharge_multiplier`, but the user later clarified the canonical unit is CNY paid per USD credit. The implemented account cost is therefore `upstream_group_rate * upstream_cost_per_usd`, rounded to four decimal places; the inverse sub2api setting is normalized only for local cost comparison.
- Upstream recharge multiplier precedence is discovered value, then manually saved value, then a `1.0` default. The default must retain an explicit default source/status and must never be persisted or presented as discovered.
- `upstream-ops` v0.0.6 (`6b90a4f`) confirms the source Sub2API user APIs: `/auth/me` for balance, `/payment/checkout-info` for `balance_recharge_multiplier`, `/groups/available` for base group rates, and `/groups/rates` for user-specific overrides.
- For New API/One API sources, status/self/groups endpoints use `quota_per_unit`, `price`, user quota, and `{groupName: {ratio, desc}}`; the adapter must skip nonnumeric ratios such as `auto`.
- Recharge detection failures must keep the last applied sub2api rate unchanged. Falling back to raw group ratio and writing it would silently misbill users.
- The local target sub2api currently contains 43 accounts, including 24 API-key accounts across OpenAI and Anthropic, with account `rate_multiplier` values already in use. The new page must import/list these without changing them until an explicit successful sync.
- Existing sub2api account responses omit the stored `api_key` while exposing non-sensitive credential fields such as `base_url`; legacy accounts therefore require an explicit one-time key binding before direct upstream group/recharge discovery.
- Chosen model boundary: one local managed row per unique `sub2api_account_id`, with encrypted upstream key, normalized upstream URL, selected group, last successful non-secret rate/balance snapshot, source/status/error fields, and timestamps.
- Chosen source behavior: balance can be refreshed through the target sub2api's server-side `/accounts/:id/balance` even before a key is bound locally; group and recharge discovery use a site adapter only after key binding.
- Chosen apply behavior: calculate with `Decimal`, reject nonpositive recharge multipliers, quantize half-up to four decimals, and write through sub2api bulk-update only after all required source values are valid.
- Security review requirements applied: all routes require the existing admin session; no full key/ciphertext in API responses, logs, DOM, or errors; outbound URLs reject unsafe schemes/private targets; redirects and environment proxies are disabled; response sizes and timeouts are bounded.
- Referenced-session acceptance used an explicit dry-run before writeback and confirmed 24 accounts across 7 upstream sites; source/status enums and default recharge semantics are now part of the API contract rather than frontend-only labels.
- Live target sub2api `payment_balance_recharge_multiplier` is `10.0`, available from authenticated `GET /api/v1/admin/settings`.
- Live balance verification: account `2232` (`www.cattoken.cc`) returns `unsupported` with no numeric fields, while account `2250` (`zz1cc.cc.cd`) returns `ok`, unit `USD`, and numeric remaining/total/used values including a negative remaining balance. The UI must not conflate unsupported with zero or clamp valid negative balances.
- The earlier Plus/sample/identity work was committed as `92af35a`; shared backend/frontend files are now clean, so this feature can make minimal direct integration edits without racing an active worktree change.
- Existing `redact()` masks every character except the final four and secret encryption derives a Fernet key from `APP_ENCRYPTION_KEY`; the new API should reuse both and offer no reveal endpoint.
- Backend review caught that balance parsing must accept finite negative values and only update `balance_checked_at` after a successful balance response; the implementation and regression tests were adjusted accordingly.
- Pydantic/FastAPI validation errors include the rejected `input` value even with `hide_input_in_errors=True`; API-key/token length validation must therefore occur in the service with a generic error rather than a secret-bearing schema max-length failure.
- Frontend review identified that a failed/unsupported latest balance probe should still show the last successful numeric balance as stale, rather than hiding it; status and error text remain visible beside the retained amount.
- Final recharge semantics are endpoint- and field-aware: NewAPI payment config/checkout explicit multipliers win; only a missing explicit value may fall back to `1 / status.price`; failed or invalid discovery never becomes the default `1x`.
- Existing sub2api API-key accounts can opt into balance monitoring without copying their upstream key into this plugin. A user-triggered probe creates a no-key local row and uses sub2api's server-side balance endpoint; direct group/recharge discovery remains unavailable until credentials are explicitly bound.
- Direct credentialed discovery requires HTTPS. The client validates every resolved address as public, pins the TCP connection to a validated IP, preserves the logical Host/TLS SNI, disables redirects and environment proxies, and tries each address from one DNS result at most once.
- All upstream-derived strings are scrubbed against stored credentials before persistence and response construction. Route validation also redacts malformed `api_key`, `access_token`, and credential-bearing `base_url` inputs from FastAPI 422 details.
- Configuration identity changes clear old automatically selected groups and invalidate all derived previews. Applying a rate always re-discovers, compares the four-decimal confirmation, performs an exact one-account update, and verifies the readback.
## 2026-07-13 Sample Page Window Grouping

- The sample API exposes a flat `windows` collection whose rows already carry `window_key`, subscription metadata, calibration, and samples.
- The frontend currently creates one top-level tab per `window_key + plan_cohort`, so quota windows are fragmented by subscription type instead of grouped under 5h, 7d, and monthly views.

## 2026-07-13 Upstream Channel Cards and Balance Repair

- The old balance path is definitively account-scoped: it calls local sub2api `/admin/accounts/{id}/balance`, so those snapshots cannot represent a shared upstream wallet.
- Correct NewAPI site balance is `/api/user/self.data.quota / /api/status.data.quota_per_unit`; `used_quota` is cumulative usage, and recharge cost must not be applied to either number.
- Correct Sub2API site balance is `/api/v1/auth/me.data.balance` in USD.
- Both direct balance contracts require the channel login Access Token; a model API key must never substitute for it. NewAPI additionally requires a positive numeric `New-Api-User` value.
- The additive SQLite migration groups the 24 preview configs into seven canonical URLs, retains legacy NOT NULL columns, and deliberately initializes every channel balance as `not_checked`.
- URL canonicalization removes one exact trailing `/v1` or `/api/v1`, normalizes scheme/IDNA host/default port, and preserves all other path prefixes.
- Chrome still lists the logged-in `ZZ API Console` wallet tab and the 哈基米 login tab, but claiming the existing ZZ tab timed out a second time even when only URL/title were requested; no page content or credential was read.
- A fresh controlled Chrome tab inherited the existing ZZ login state. Cloudflare's automatic check completed without interaction and the tab reached `https://zz1cc.cc.cd/wallet` with title `ZZ API Console`.
- The visible ZZ wallet page reports a current balance of `$502.19` and exposes a `/profile` navigation entry. No browser storage or token was inspected.
- ZZ profile exposes a visible `访问令牌` button described as generating/managing API access tokens. The visible profile does not yet show a numeric New-Api-User ID.
- Opening the supported ZZ Access Token dialog started its built-in token generation flow. The token field was not read or emitted; the dialog still did not expose a numeric user ID.
- ZZ generated a 32-character system Access Token through the visible dialog. It was immediately regenerated for the test session and retained only in browser-session memory; no token value is persisted in planning files or terminal commands.
- A focused visible navigation to ZZ's known `/api/user/self` endpoint was blocked by the browser client, so it could not be used to obtain the numeric user ID. No storage or cookie fallback was attempted.
- The final ZZ test token was regenerated again after verification and retained only in browser memory; only its non-secret populated/length metadata was observed. Numeric user ID remains unresolved through visible UI.
- Closing the token dialog returned to the same profile view; the only profile tabs are `账户绑定` and `设置与偏好`, with no numeric user ID visible in the selected account-binding view.
- ZZ `设置与偏好` exposes notification and account-behavior settings only; it also contains no numeric user ID.
- The visible ZZ avatar/account-menu control did not expose any additional menu content or numeric ID in the accessible page state.
- Live ZZ discovery now reaches the public NewAPI endpoints through the narrow fake-IP DoH fallback. Its `status.price` is `0.0621`, which is already the normalized CNY/USD cost; the authenticated self endpoint still rejects the token without `New-Api-User`.
- Observing the visible ZZ token dialog's `/api/user/token` request exposed the required numeric `New-Api-User` header without reading browser storage. With the current system token, `/api/user/self` returns `$502.1853`, matching the wallet's rounded `$502.19`.
- Hakimi is Sub2API and exposes no user-facing long-lived token generator. Its supported credential is the login JWT used as `Authorization: Bearer ...`; a visible dashboard refresh supplied it without reading browser storage, and `/api/v1/auth/me` returned `$46.226`.
- Hakimi's `/api/v1/payment/checkout-info` reports `balance_recharge_multiplier=1`, normalized to `1 CNY/USD`, and its group endpoints returned 11 groups.
- A fresh Chrome tab reached the 哈基米 root page with title `哈基米biu - AI API Gateway`; its initial SPA snapshot was still empty.
- After the 哈基米 SPA loaded it redirected to `/home` and exposed only `登录` / `立即开始`; the Chrome profile is not currently authenticated to this site.
- The updated local preview is reachable at `http://127.0.0.1:5174/` after the backend restart; the Chrome test tab initially entered the app loading state.
- Local preview login succeeds with the isolated preview key and the `API Key` navigation entry is available from the authenticated dashboard.
- The rewritten API Key view renders its channel-level summary, URL-grouping explanation, channel actions, search/filter controls, and loading state without the old table tree.

- The in-app browser contains only duplicate local preview tabs; the relevant existing login state is in Chrome.
- Chrome has an existing `https://zz1cc.cc.cd/wallet` tab titled `ZZ API Console`, matching the 章鱼哥/`zz1cc.cc.cd` NewAPI candidate from the local account inventory.
- Chrome has an existing `https://api.biuapi.com/login` tab titled `登录 - 哈基米biu`, matching the requested 哈基米 Sub2API channel, though login state still needs verification.
- Browser policy prohibits reading cookies, Local Storage, session stores, profiles, or passwords. Credential acquisition must use visible site UI or a supported authenticated page action without extracting browser storage.
- Current persistence puts `base_url`, protocol, encrypted API key/access token, recharge discovery, and balance fields directly on every `UpstreamAccountConfig`; grouping requires a real channel owner rather than a frontend-only visual regroup.
- Current discovery always starts an account-level sub2api balance call and stores that result on the account, while direct upstream discovery returns only group/recharge metadata. Channel-level balance needs a direct authenticated upstream balance result in the discovery adapter.
- Current public API is a flat `list[UpstreamAccountOut]` plus account-scoped PUT/discover/apply/delete routes. A compatibility-preserving response can add channel DTOs/routes while retaining account apply endpoints.
- SQLite migrations are handled imperatively in `init_db` alongside `create_all`; a new channel table plus nullable account `channel_id` can be created safely, then existing rows can be backfilled by normalized URL.
- The current React page is a desktop table with separate mobile cards and an account-scoped configuration dialog. The requested design should replace both with one responsive channel-card grid containing compact account cards, avoiding duplicated desktop/mobile render trees.
- `UpstreamClient._normalize_base_url` already removes trailing `/v1` and `/api/v1` before direct requests, but the account-service normalization used for persisted/grouped URLs does not. Channel identity should reuse one canonical normalizer across persistence, migration, and API input.
- The discovery adapter currently has no balance fields or direct balance parser at all. The visible balance comes only from sub2api's account-scoped `/accounts/:id/balance`, which explains why multiple same-site accounts duplicate balance and why NewAPI site balance can be wrong for channel semantics.
- The `upstream-ops` Git worktree is intentionally missing files but commit `6b90a4f` remains readable through `git show`; do not restore or alter this reference directory.
- `upstream-ops` channel cards use a responsive 1/2/3-column grid, a compact status header, balance/cost stat tiles, two-line collapsible group-rate chips, and an icon action row. This visual language should be adapted to channel cards with nested compact account cards.
- NewAPI direct balance contract: public `GET /api/status` supplies `quota_per_unit` (default 500000) and `price`; authenticated `GET /api/user/self` supplies `quota` and `used_quota`; after unwrapping `{success,data}`, USD balance is `quota / quota_per_unit`.
- NewAPI token auth requires both a system access token in a raw `Authorization` header and `New-Api-User`; the reference deliberately does not add `Bearer`. Cookie mode exists in the reference but browser policy prevents harvesting cookies, so this implementation should guide users to generate the visible system access token instead.
- Sub2API direct channel balance uses authenticated `GET /api/v1/auth/me` and its `balance`; group/rate/recharge endpoints are `/groups/available`, `/groups/rates`, and `/payment/checkout-info` under `/api/v1`.
- Channel balance must remain USD credit. Recharge cost (`CNY/USD`) is separate metadata and must not be applied to the displayed balance; optionally a monetary cost equivalent can be derived separately.
- Access-token guidance should distinguish NewAPI's personal-settings system token + User ID from Sub2API's Access Token (and optional Refresh Token). Current scope can store only the access token unless refresh support is added deliberately.
- The API deliberately emits default calibration entries for every configured subscription/window combination, including entries with `samples=[]`; these are the source of visible empty tabs.
- Sample collection writes `monthly` only when `_window_kind()` resolves an upstream window as truly monthly. Existing coverage proves a Plus account with a real 10,080-minute 7d window stays in `seven_day`, while explicit monthly-only usage is stored under `monthly`.
- Preserve per-subscription calibration semantics inside each quota-window view, but derive the three top-level groups only from entries with samples. This makes the monthly count include only persisted true-monthly samples and hides every empty quota-window group.
- Playwright fixture coverage showed `5h 2`, `7d 1`, and `月 1` in fixed order; the monthly view rendered only its Team true-monthly sample despite an empty Plus monthly calibration entry. With the 7d fixture emptied, the live tab list became only `5h 2` and `月 1`.
- At a 390x844 viewport, the document and body scroll widths both remained 390px. Browser console errors were exclusively expected 401 responses from unrelated protected endpoints left unmocked; no component/runtime exception was present.

## Account Identity Across Dashboard and Usage Views (2026-07-13)

- The requested extension covers three existing surfaces: overview recent accounts, overview rate-limit account rows, and the usage account-detail table.
- The shared worktree already contains unrelated uncommitted upstream-management/sample-grouping changes, including edits to `App.tsx`, `styles.css`, schemas, services, and types. New changes must be additive and narrowly scoped.
- `Overview` renders recent accounts through `AccountRow compact`; that branch currently shows only `account.email`.
- `RateLimitedAccountColumn` receives full `Account` rows but currently renders email on the first line and sub2api ID below it, so it can use `account.account_name` without an API change.
- `AccountUsageEstimateOut` and the frontend `AccountUsageEstimate` type currently expose email but not account name. The usage estimator must add a display-only `account_name` field derived from the same sub2api helper with email fallback.
- The compact recent-account branch has a two-column grid for identity plus status badges, so a single ellipsized identity wrapper can render `名称 | 邮箱` without changing the status layout.
- Rate-limit rows already reserve the right side for status/recovery badges. Replacing the left email/sub2api-ID stack with one compact identity line saves vertical space as requested; the account ID can remain available in the row tooltip if needed.
- The usage-detail table's first column is already 230px wide and currently holds one copyable email. It can reuse the existing stacked account identity styles/buttons used by the main accounts table after the API adds `account_name`.
- `_account_estimate()` already receives `Sub2ApiClient` and the raw account. It can add `account_name=sub2api.account_name(account) or email` directly beside the existing email field, with no persistence or OAuth identity changes.
- Existing backend tests exercise estimator dictionaries rather than constructing `AccountUsageEstimateOut` directly, so one focused assertion on the returned account row is sufficient to guard the new contract.
- No test directly calls `_account_estimate()` or `build_usage_estimate()`. Existing `Sub2ApiClient.account_name()` coverage already verifies name extraction/fallback boundaries; focused schema/estimator output coverage can be added without modifying unrelated FakeSub2Api fixtures.
- The CLI route shorthand stripped quotes from the first Playwright JSON fixtures; structured `route.fulfill({ json })` fixtures restored authenticated rendering without changing product code.
- Overview recent-account and rate-limit rows expose two independent copy targets while preserving the requested compact single-line `name | email` layout.
- Desktop measurements found no overlap between identity and status/recovery badges, even with `K12 Campus Account - East District | student.oauth@example.edu`.
- Usage details expose the account name above the email for both normal Plus and rate-limited K12 tabs, and both values copy independently.
- At 390px, `documentElement.scrollWidth`, body width, and viewport width all remained 390px. The usage table uses its existing internal horizontal scroller rather than widening the page.
- For the deployment commit, `backend/app/services/usage_estimate.py` and `backend/test_usage_estimate.py` contain only identity-related changes and can be staged whole.
- `backend/app/schemas.py`, `frontend/src/types.ts`, `frontend/src/App.tsx`, and `frontend/src/styles.css` also contain unrelated upstream-channel work; only their identity-specific hunks/lines may enter the commit.
- The identity-only `App.tsx` hunks are the rate-limit compact identity, recent-account compact identity/badge wrapper, usage table header/cell, and the two shared identity renderers. API-key navigation/import/view/title hunks are unrelated.
- Reconstructed the identity change from clean base `885e603` in a detached verification worktree. Its complete diff is six files, 128 insertions, and 19 deletions, with no upstream-channel imports, routes, models, types, or styles.
- The staged patch has the same stable patch ID (`c2c03f831fcd60b8bdab44d04991568becad2b7f`) as the clean verification worktree and passes an explicit upstream/API-key exclusion scan.
- x1 is exactly at the commit parent `885e603`, its tracked worktree/index are clean, and the only worktree item remains the pre-existing `01-login.png`.
- x1 services are `gptcheckplugin.service` (backend, port 8000) and `gptcheckplugin-frontend.service` (Vite preview, port 5173); both are active/running and both HTTP probes pass.
- The root filesystem has 4.7 GB free. The deployment is code/schema-output only, with no database migration or data mutation, so a Git rollback ref plus the exact parent commit is sufficient rollback coverage.
- The verified incremental bundle SHA-256 is `02517ad43850e1f0dc8198e81632f0b5fe36e4e3edadfefb7b253837f2f14143`; x1 fetched exact target `c8f1fbe` and created rollback ref `refs/backups/pre-identity-c8f1fbe-20260713T061217Z` before clean-tree verification.
- Neither x1's system Python nor production virtualenv includes `pytest`; remote regression tests must use an isolated temporary test virtualenv so production dependencies remain unchanged.
- Remote clean-tree verification at `c8f1fbe` passed 47 backend tests, `npm ci`, zero-vulnerability production audit, and the Vite production build; both temporary worktree and isolated pytest target were removed.
- Production x1 fast-forwarded to `c8f1fbe`; both systemd units are active and backend health, frontend, authenticated login, and usage-estimate endpoints all return HTTP 200.
- The production usage-estimate response contains 60/60 rows with both `account_name` and `email`; 58 currently have distinct values. No identity values or credentials were printed during verification.
- Production frontend assets are the clean-build hashes `index-BDqbCuNY.css` and `index-BO-O-0fL.js`, and the only remaining x1 worktree item is the preserved `01-login.png`.
- A separate secondary-navigation workflow advanced local `main` to descendant `166ed56` after this commit; x1's final readback remains exactly `c8f1fbe`, and Git confirms the identity commit is present.
## 2026-07-13 Commit and Deploy Sample Grouping to x1

- Local `main` starts at `504a977`; the worktree is dirty with unrelated API-key, identity, backend, test, and planning changes.
- `frontend/src/App.tsx` contains both unrelated changes and the sample grouping implementation, so whole-file staging is unsafe. The commit must use hunk-scoped cached staging and staged-diff verification.
- Known x1 target: SSH `root@154.12.51.74:54321`, repository `/root/apps/GptCheckPlugin`.
- x1 preflight is exactly at local base `504a977`; its only worktree item is the pre-existing untracked `01-login.png`. Both services are active, backend health returns OK, and frontend returns HTTP 200.
- Local frontend production build passes on the complete working tree. No staged changes existed before hunk isolation.
- Commit `885e603` contains only three `UsageLimitSamplesView` hunks in `frontend/src/App.tsx` (65 insertions, 50 deletions). No unrelated file or hunk entered the commit.
- A clean detached worktree at `885e603` passed `npm ci`, full dependency audit with zero vulnerabilities, TypeScript compilation, and the Vite production build.
- The verified incremental bundle is 1,352 bytes, advertises `main=885e603`, and requires x1's exact `504a977` base. It was transferred to `/tmp` and fetched into `refs/remotes/deploy/sample-grouping-885e603`.
- x1 clean temporary worktree at `885e603` passed `npm ci`, zero-vulnerability audit, TypeScript compilation, and Vite build. Node 18 emitted an engine warning for `@vitejs/plugin-react@5.2.0`, but the unchanged dependency set built successfully.
- Production fast-forward/build completed at `885e603`; `dist` serves the expected `index-eNTarn2Z.js` bundle, both services are active, and the backend remains healthy.
- A secret-safe authenticated production probe returned login/sample API status 200 and aggregate counts `five_hour=157`, `seven_day=39`, `monthly=14`, confirming all three top-level groups have real samples on x1.
## 2026-07-13 Sample Subscription Secondary Navigation

- `sampleWindowGroups` already contains only non-empty subscription cohorts, so `group.cohorts.length` is the exact visible subscription-type count and `group.sampleCount` remains the aggregate sample count.
- Add a second selected key for `plan_cohort`; synchronize it against the selected primary group's cohorts and render one selected cohort panel instead of mapping every cohort panel vertically.
- The existing rate-limited accounts panel precomputes separate 5h, 7d, and monthly account arrays but always renders all three columns. A filtered array plus selected-key fallback can hide empty window buttons without backend changes or effect-based synchronization.
- Both implementations compile successfully. Playwright verification should use accounts with 5h/month rate-limit keys and no 7d key, plus samples spanning multiple subscriptions, to prove absent filters and secondary selection behavior.
- The browser fixture can authenticate locally and fulfill the initial dashboard/settings/account requests without database writes. Planned assertions: rate-limit buttons `5h 2` and `月 2` with no 7d; sample primary tabs with both sample/subscription counts; secondary Plus/Team switching changes the single rendered panel.
- Playwright confirmed the rate-limit panel renders only `5h 2` and `月 2`, omits empty 7d, and swaps the account list on selection.
- Sample navigation confirmed primary metrics `5h 3 samples / 2 subscriptions`, `7d 1 / 1`, `monthly 2 / 2`; secondary navigation switched Plus to Team and changed the only rendered panel, while primary monthly selection reset to valid Team/K12 options.
- At 390px, primary/secondary tabs wrap cleanly and both document/body scroll widths equal the viewport width.
## 2026-07-13 Commit and Deploy Secondary Navigation to x1

- Local `main` and x1 independently advanced to the same `c8f1fbe` identity-display commit before this deployment request; use that as the new exact base rather than the earlier `885e603` assumption.
- The dirty working CSS combines this task with unrelated API-key styles, so use a clean temporary worktree/branch to recreate and commit only the intended App/CSS changes.
- Preserve the existing detached identity verification worktree at `C:/Users/zanez/Documents/agents_playground/GptCheckPlugin-identity-verify-885e603` because it was not created by this deployment workflow.
- Clean-worktree diff contains only `frontend/src/App.tsx` (navigation state/rendering) and `frontend/src/styles.css` (110 lines of matching styles). `npm ci`, zero-vulnerability audit, TypeScript, and Vite build passed.
- Isolated commit `166ed56 feat(quota): add secondary window filters` contains exactly those two frontend files (198 insertions, 38 deletions).
- Verified 2,589-byte incremental bundle advertises `main=166ed56` and requires exact base `c8f1fbe`. x1 fetched it into a temporary deploy ref and passed clean-worktree `npm ci`, zero-vulnerability audit, and production build.
- Production `main` is now `166ed56`; authenticated sample/account probes confirm the new navigation has real counts and the non-empty rate-limit rule hides monthly because its current raw count is zero.
## 2026-07-13 Usage Detail Rate-Limit Window Filters

- The requested location is `UsageEstimateView` → `账号额度明细`, not the overview `限流账号` panel.
- Detail rows use `AccountUsageEstimate`, which carries `rate_limited_windows` plus `five_hour/seven_day.rate_limited` and `window_kind`. Reusing `accountRateLimitedWindowKeys(account, account)` provides the existing monthly normalization semantics.
- Place the dynamic window tabs inside the left title block below its subtitle, then filter `detailBaseAccounts` by the selected non-empty window before deriving subscription options.
- Deriving tab counts from visible rate-limited detail accounts ensures a button is absent when no matching rows can be shown.
- Clearing the subscription selection on account-type and window changes prevents a valid new window from appearing empty because of a stale subscription filter.

## 2026-07-13 Sub2API Refresh Token Contract

- `POST /api/v1/auth/refresh` is unauthenticated apart from the JSON `refresh_token`; success returns `data.access_token`, `data.refresh_token`, `data.expires_in`, and `data.token_type`.
- Sub2API deletes the old RT before generating the new pair, so the plugin must persist both returned tokens before retrying any balance or group endpoint.
- Only a 401 from `/api/v1/auth/me` is a reliable expired-login-token signal. A 401 from another group/key endpoint must not consume the single-use RT, and NewAPI must never enter this flow.
- Refresh requests reuse the discovery client's normalized HTTPS URL, public-IP validation, DNS pinning, disabled redirects, bounded response reader, and secret-safe error handling.
- Hakimi live verification exercised the complete path: invalid local AT -> real `/auth/me` 401 -> RT rotation -> encrypted pair commit -> successful retry with 11 groups and a current positive USD balance.

## 2026-07-13 Upstream API Key Group Matching

- The local sub2api list intentionally redacts API Keys, but authenticated `GET /admin/accounts/data?ids=...&include_proxies=false` returns ordered credential exports. A count mismatch must reject the entire batch to prevent account/Key misassociation.
- NewAPI token lists can return the same masked records from both list and search endpoints; deduplicate by positive token ID before testing uniqueness.
- NewAPI `POST /api/token/{id}/key` returns an internal raw Key that can omit the standard `sk-` prefix and, for custom keys, may differ from the displayed/callable Key. Prefer canonical exact matching, then allow only a fully anchored mask with at least four visible characters on each side (or the known `sk` mask form) and a one-to-one mapping in both directions.
- Sub2API `GET /api/v1/keys/{id}` may return the full Key without the list record's `group_id`; merge detail fields over the listed record before resolving the group.
- Octopus `used_quota=5,057,755,384` and `quota_per_unit=500,000` produce `$10,115.510768`, displayed as `$10,115.5108`. This is cumulative upstream user-account usage, not plugin-generated usage and not `$10`.

## 2026-07-13 Commit and Deploy Usage Detail Filters to x1

- Local `main` and x1 both start at exact commit `166ed56`; the local index and x1 tracked worktree/index are clean.
- x1 preserves only the pre-existing untracked `01-login.png`, has about 4.6 GB free, and both systemd services are active.
- The backend health response is OK and the frontend returns HTTP 200 before deployment, so a fast-forward plus Git rollback ref is sufficient for this frontend-only change.
- Incremental bundle SHA-256 is `410c85cb2898fa3cac412cabf02ed94e3b6984832eb977a33e4a89acce694110`; it advertises target `f8c48df` and requires exact base `166ed56`.
- x1 fetched the exact target, created rollback ref `refs/backups/pre-usage-detail-f8c48df-20260713T075905Z`, and passed a clean-worktree zero-vulnerability audit and production build.
- x1 Node 18 emits the existing `@vitejs/plugin-react@5.2.0` engine warning, but the unchanged dependency set compiles successfully.
