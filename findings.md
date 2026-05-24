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
