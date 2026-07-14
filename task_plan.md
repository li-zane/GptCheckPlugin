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

## Change Request: Memory Guard and Split Refresh Concurrency

- [x] Explain high refresh-job memory peaks from browser process-tree RSS sampling.
- [x] Add a browser fallback guard that skips Playwright when available system memory is below the configured threshold.
- [x] Split refresh concurrency settings for protocol refresh work and browser login work.
- [x] Expose the new settings through backend schemas/runtime settings and the React settings page.
- [x] Compile/build-check backend and frontend.

## Latest Errors Encountered

- Tried to run `.venv/bin/python` from the `backend` directory; that relative path does not exist there. Re-ran the memory-helper smoke check from the project root with `PYTHONPATH=backend`.
- Tried to define `async def` directly after semicolons in `python -c`; Python rejected the syntax. Re-ran the settings smoke check with `exec(...)`.

## Change Request: Recovery Toggle and File-backed Settings

- [x] Add a settings-page toggle for the post-refresh sub2api recovery behavior.
- [x] Add a settings-page toggle for the account recovery task itself.
- [x] Make runtime sub2api config read the recovery toggle dynamically.
- [x] Persist admin-panel settings to the project-root `.env` file.
- [x] Make `Settings` always load the project-root `.env`, independent of uvicorn working directory.
- [x] Compile/build-check backend and frontend.

## Change Request: Account and Mailbox Search

- [x] Add local search to the account list.
- [x] Add local search to the mailbox credential list.
- [x] Style search controls to match existing toolbar and panel design.
- [x] Build-check frontend.

## Change Request: Skip Existing Mailboxes on Import

- [x] Detect existing mailbox credentials before importing.
- [x] Skip rows whose GPT email or retrieval mailbox email already exists.
- [x] Skip duplicate GPT/retrieval mailbox emails inside the same import payload.
- [x] Keep invalid-line reporting and include duplicate skips in the skipped count.
- [x] Compile-check backend.

## Fix: Bulk Delete Problem Accounts Button

- [x] Diagnose why the top-right delete button was disabled.
- [x] Keep ordinary remote error accounts non-deletable because they may recover after reauthorization.
- [x] Make the frontend count only deactivated accounts and duplicate abnormal accounts with a safe primary replacement.
- [x] Make the bulk delete endpoint delete only deactivated accounts and duplicate abnormal accounts with a healthy primary replacement.
- [x] Compile/build-check backend and frontend, and smoke-check duplicate cleanup eligibility.

## Change Request: OAuth Refresh Token Acquisition

- [x] Add OpenAI OAuth PKCE token exchange that can obtain and write a GPT `refresh_token`.
- [x] Probe protocol automation for OAuth login and keep it behind an experimental toggle because the consent page currently requires JS.
- [x] Reuse the existing mailbox email-code reader and stop immediately on add-phone / phone-number-required pages.
- [x] Preserve memory safety by using the existing browser memory guard before Playwright OAuth fallback.
- [x] Verify with compile checks and cautious single-account smoke tests for `annamason5243@outlook.com`.

## Change Request: Port mail-console-x1 GPT RT/AT Refresh

- [x] Compare the current plugin refresh flow with `mail-console-x1` direct OpenAI RT/AT refresh implementation.
- [x] Add secure local storage for GPT OAuth tokens needed for direct OpenAI refresh.
- [x] Port a minimal OpenAI token/profile refresh service into the plugin backend.
- [x] Integrate the new local RT/AT paths into refresh orchestration without removing existing fallbacks.
- [x] Run migration/compile/static verification and record the outcome.

## Fix: Usage Estimate Availability

- [x] Diagnose why many accounts showed as not estimable.
- [x] Use current official window raw usage, not only baseline delta, to estimate remaining quota.
- [x] Keep baseline delta as display-only "new usage since enable" information.
- [x] Use cached usage-window state when the accounts page loads estimates with `refresh=false`.
- [x] Compile/build-check backend and frontend.

## Change Request: Calibrated Usage Quota Display

- [x] Add local 5h/7d limit sample storage for accounts observed at or near usage limits.
- [x] Keep at most the middle 100 samples per window after sorting by observed limit.
- [x] Clamp inferred window totals to a 3 sigma range once 100 samples exist; before that, use 5h `$15-$25` and 7d `$100-$140` default ranges.
- [x] Update sample collection when active usage-window data is fetched.
- [x] Replace per-account quota text with progress-bar usage display and remove "新增" from the UI.
- [x] Compile/build-check backend and frontend.

## Change Request: Usage Estimate History

- [x] Replaced by the usage limit sample page request below; UI/API/save logic removed.

## Change Request: Usage Limit Sample Page and Fixed Sidebar

- [x] Remove the aggregate usage estimate history panel and API added in the prior step.
- [x] Add an authenticated usage-limit-samples API exposing the current 5h/7d sample rows and calibration range.
- [x] Add a standalone `样本` page showing the up-to-100 rows used for official-window quota estimation.
- [x] Fix layout so the sidebar stays fixed while the right workspace scrolls independently on desktop.
- [x] Compile/build-check backend and frontend, run database migration validation, validate sample response model, and restart services.

## Change Request: Account Rate-Limit Distinction

- [x] Reuse existing 5h/7d rate-limit detection from usage-window/sample logic.
- [x] Expose `rate_limited` and `rate_limited_windows` in account and usage-estimate API responses.
- [x] Show rate-limited accounts as `限流`/warn in the account list and make account search match `限流` / `rate limited`.
- [x] Mark limited quota progress bars with warning styling.
- [x] Split account-list limit labels into separate `5h限流` and `7d限流` items and show estimated recovery timing below each item.
- [x] Compile/build-check backend and frontend, run helper smoke test, and run `git diff --check`.

## Incident: Refresh Failures and Backend Restart Loop

- [x] Inspect latest refresh jobs, app events, systemd state, and backend journal.
- [x] Confirm the restart loop was caused by SQLite lock exceptions during refresh memory/event writes, followed by an orphaned uvicorn process keeping port `8000` occupied.
- [x] Make refresh finalization/event writes non-fatal and split critical job-state commits from optional app-event commits.
- [x] Enable SQLite busy timeout and WAL mode to reduce write-lock contention.
- [x] Delay scheduled monitor sync briefly after startup so a failed port bind cannot enqueue refresh jobs before uvicorn exits.
- [x] Terminate the stale backend process, restart `gptcheckplugin.service`, and verify health, port ownership, and refresh-job convergence.
## Change Request: x1 Sync and Extensible Subscription Handling (2026-07-13)

- [x] Phase 1: Locate the x1 project copy, compare it with this repository, and preserve local/user changes.
- [x] Phase 2: Trace OAuth subscription detection, labels, filters, usage windows, sample persistence, and settings contracts.
- [x] Phase 3: Sync the applicable x1 code into this project.
- [x] Phase 4: Introduce forward-compatible subscription normalization with explicit K12 support and unknown-type fallback.
- [x] Phase 5: Extend labels, filters, sample records, and settings-configurable default quota ranges for every detected type.
- [x] Phase 6: Add focused backend/frontend tests, security checks, builds, and runtime UI verification.

### Acceptance Criteria

- K12 OAuth accounts are recognized and processed instead of falling through existing plan-specific logic.
- Future subscription strings remain visible and usable through normalized type metadata and an `unknown`/derived fallback.
- Subscription type is consistently represented in API responses, UI labels, filters, and persisted usage samples.
- Default quota ranges are configurable per subscription type without code changes for newly discovered types.
- Existing account/token secrets are not logged, exposed, or weakened by the implementation.

### Errors Encountered

- First multi-file append used an anchor that existed only in `progress.md`; `apply_patch` rejected the entire patch without modifying files. Resolved by using each file's actual final line.
- First x1 discovery command failed before remote execution because nested PowerShell/zsh double quotes were unmatched. Switched to a single-quoted, simplified remote command.
- Remote file-list filtering used a regex that zsh parsed incorrectly, causing that parallel read batch to fail. Removed remote regex/pipes and switched to simple Git commands plus local tree analysis.
- Fast-forward to x1 `9270805` succeeded, but restoring the three planning files produced append/append conflicts. Resolved by retaining both x1 history and this task's appended sections.
- PowerShell misparsed an unquoted `stash@{0}` during cleanup. Retried with a quoted stash reference, then unstaged the resolved planning files.
- First estimator regression run had one expected failure because the old test required monthly samples to be Team-only. Updated the obsolete expectation and added K12/future-type coverage.
- Full unittest discovery exposed a circular import because schema validation imported a module under `app.services`, whose package initializer imports monitor/schema code. Moved the pure module to `app.core.subscription_types`.
- Playwright found that the existing 12-second dashboard poll replaced the settings object and reset unsaved form edits. Changed polling updates to preserve settings object identity when values are unchanged.
- Final npm audit reported one high-severity Vite development-server issue and one low-severity Babel issue, both with a compatible non-force fix. Applying `npm audit fix` and re-verifying the build/audit.
- Isolated API smoke testing showed startup port scanning also writes `.env`. Centralized the `APP_ENV=test` guard inside `_persist_settings_file()` so every persistence path is covered.

### Final Verification

- Backend: 38 unit tests passed; `compileall` passed.
- Frontend: TypeScript/Vite production build passed with Vite 6.4.3.
- Security: no hardcoded secret patterns found; `npm audit --omit=dev` reports 0 vulnerabilities.
- UI: Playwright desktop/mobile snapshots passed; mobile document width equals the 390px viewport and all quota windows remain visible.
- Runtime: isolated backend/frontend are healthy on `127.0.0.1:8000` and `127.0.0.1:5173`.

## Fix: Monthly Usage Missing Cost Display

- [x] Reproduce `niubi963019@edu.aiceo.dev` monthly-window output against live sub2api usage.
- [x] Confirm sub2api returns monthly `utilization=100` with `cost=0`, so the plugin only has a percent signal and no actual spent amount.
- [x] Stop converting percent-only monthly quota signals into `estimate_spent=estimated_limit`.
- [x] Compile/build-check backend and frontend.

## OAuth Account Name and Email Identity (2026-07-13)

- [x] Trace the sub2api account-name source through the accounts API and frontend table.
- [x] Keep email as the OAuth identity while exposing a separate display-only account name.
- [x] Show account name above email and make both values independently copyable.
- [x] Verify backend tests, frontend build, search behavior, both copy actions, and desktop/mobile layouts.

## Plus Weekly Window and Sample Management (2026-07-13)

- [x] Phase 1: Inspect x1 Plus usage payloads, persisted samples, service state, and current refresh-window classification.
- [ ] Phase 2: Correct Plus refresh-time/window handling so seven-day limits are not presented as monthly.
- [x] Phase 2: Correct Plus refresh-time/window handling so seven-day limits are not presented as monthly.
- [x] Phase 3: Derive non-Team monthly defaults from four times the corresponding weekly range.
- [x] Phase 4: Add an authenticated, validated single-sample deletion API and a compact manual-delete control on the samples page.
- [x] Phase 5: Add regression/security coverage and verify backend, frontend, desktop, and mobile behavior.
- [x] Phase 6: Commit the complete working tree, deploy safely to x1, remove only confirmed anomalous Plus 7d samples above $200, and verify production health/data.

### Final Verification

- Commit: `92af35a feat(accounts): improve identity and quota management`.
- Local/remote backend: 46 tests passed; compileall passed.
- Frontend: TypeScript/Vite production build passed; full npm audit reports 0 vulnerabilities.
- Security: delete API returned 401 unauthenticated, 422 for ID 0, and 404 for an authenticated missing row; changed-line secret scan passed.
- UI: Playwright verified confirmation/deletion refresh, 4x weekly-to-monthly settings linkage, and 390px document containment for settings and samples.
- x1: both services active, health API OK, frontend HTTP 200, Plus windows now `none + seven_day` with 10080-minute windows and preserved reset times.
- Data: integrity-checked pre-change SQLite backup retained; seven Plus 7d samples above $200 deleted transactionally; post-delete count is 0 and remaining maximum is $198.6886.

### Acceptance Criteria

- Plus accounts without a 5h window still expose their upstream 7d reset as a weekly window, never a monthly label solely because the reset is more than seven days away.
- Team monthly behavior remains unchanged; every non-Team subscription's default monthly lower/upper bounds equal four times its weekly defaults.
- Administrators can delete one persisted usage-limit sample from the samples page after explicit confirmation.
- The delete endpoint requires the existing admin session and accepts only a positive integer sample ID; missing rows return 404.
- x1 Plus seven-day samples with observed limits above $200 are removed after a pre-delete inventory and post-delete verification.
- The committed revision is deployed to x1 with healthy backend/frontend services and no credential disclosure.

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| Truncating the large stylesheet diff with `Select-Object -First` caused PowerShell to return exit code 1 after the useful identity hunks were printed. | 1 | Retain the captured first two identity hunks and build the isolated patch in a clean worktree instead of repeating the truncating pipeline. |
| The pre-stage guard used PowerShell truthiness on `git diff --cached --quiet`; a successful command with no stdout evaluated false and incorrectly reported a non-empty index. | 1 | Verified `git diff --cached` is empty and switched the guard to `$LASTEXITCODE` before staging. |
| Piping a Git patch through PowerShell text objects changed line-ending bytes, so `git apply --cached` rejected every hunk even though the clean worktree patch is valid. | 1 | Keep the atomic index unchanged, verify it remains empty, and pass the same patch through `cmd.exe`'s byte-preserving pipe instead of the PowerShell object pipeline. |
| The first `cmd.exe` pipeline escaped `|` as `^|` inside an already quoted command string, causing Git to receive a literal `|` revision argument. | 1 | The index remains empty; pass an unescaped pipe inside a single PowerShell string so `cmd.exe` performs the pipeline. |
| The first x1 base64 preflight used backslash quote escaping, which PowerShell does not honor; part of the Python expression executed locally and zsh saw an unmatched quote. | 1 | Build the full remote Python command as one PowerShell string using backtick-escaped quotes, then pass it as a single SSH argument. |
| OpenSSH re-tokenized the supposedly single remote command and stripped the Python `-c` quoting, so the second encoded preflight also failed before executing the script. | 2 | Stop embedding code in the remote command; launch `ssh ... python3 -` with redirected standard input through `ProcessStartInfo`. |
| The first remote clean-worktree test used the production virtualenv, which intentionally does not contain `pytest`. | 1 | The verification worktree was removed in `finally`; inspect the available remote test runner and use it without changing production dependencies. |
| The first final-cleanup verifier sent literal Chinese asset labels through PowerShell redirected stdin using a non-UTF-8 encoding, so remote Python rejected the source before execution. | 1 | Keep the script ASCII-only and express the two labels with Python Unicode escapes; no cleanup or remote state change occurred in the failed attempt. |
| The first local cleanup guard compared the registered worktree lines with PowerShell `-contains`, but Git's path representation did not exactly match the resolved Windows string. | 1 | Inspect Git's porcelain path and compare normalized full paths before invoking worktree removal; no local files were removed in the failed attempt. |
| First x1 inventory stopped after `find data` returned no matches, so the sample query did not run. | 1 | Keep the read-only inventory but remove `&&` dependence on optional directories; discover the configured database path before querying. |
| Remote zsh rejected a nested double-quoted `lsof`/environment probe. | 1 | Avoid compound shell quoting and use separate simple SSH commands plus a base64-encoded Python read-only probe. |
| First base64 Python probe could not import `app` from the repository root. | 1 | Reran the same read-only script with `PYTHONPATH=backend`, matching the systemd app-dir layout. |
| A combined settings/CSS inspection returned exit 1 because the final CSS search pattern had no match. | 1 | Retained the successful file output and reran a focused search using the actual `usage-sample-*` class names. |
| Two parallel test-search batches failed because PowerShell does not expand `backend\\test*.py` for `rg`, then because one no-match `rg` caused the aggregate call to fail. | 1-2 | Switched to direct file reads and `rg -g 'test*.py'` commands that tolerate no-match results. |
| First targeted test run retained an obsolete assertion that a `$200` Plus monthly sample meets the old `$100` lower bound. | 1 | Updated the boundary test to the new derived `$400` lower bound (`399.99` rejected, `400` accepted). |

## Change Request: API Key Upstream Account Management (2026-07-13)

- [x] Phase 1: Recover the referenced Codex task context and inspect `bejix/upstream-ops`, the current repository, and the live local sub2api API.
- [x] Phase 2: Define the persisted upstream API-key account model, balance/group-ratio discovery contract, and recharge-ratio-to-sub2api billing-ratio rule.
- [x] Phase 3: Implement authenticated backend CRUD, upstream probing/sync, safe secret handling, and sub2api ratio updates.
- [x] Phase 4: Add a dense API-key account management page consistent with the existing admin UI.
- [x] Phase 5: Add focused backend/frontend tests and verify against the running local sub2api plus representative upstream responses.
- [x] Phase 6: Run production builds, Playwright desktop/mobile flows, security checks, and record the final result.

### Backend subtask checklist

- [x] Extend the persistent model and public Pydantic contracts without exposing stored credentials.
- [x] Extend `Sub2ApiClient` with API-key listing, balance/settings/current-rate/update methods.
- [x] Implement encrypted upsert/delete, dry-run discovery, confirmed apply, and per-account locks.
- [x] Register admin-only routes and add focused regression/security tests.
- [x] Run targeted/full backend tests, compile/diff checks, and a changed-line secret scan.

### Acceptance Criteria

- Administrators can add, edit, test, sync, and remove upstream API-key accounts without exposing full API keys in responses or logs.
- A sync reads the upstream site's available balance and group multiplier options, persists the selected/current values, and displays them on the management page.
- The effective sub2api billing multiplier is derived deterministically from the upstream group multiplier and configured recharge multiplier, then written to the matching sub2api account.
- Upstream failures are isolated per account and surfaced with actionable status without corrupting the last successful values.
- The feature works with the locally running sub2api and remains responsive on desktop and mobile.

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| `read_thread` rejected the referenced task ID, both with and without `hostId=local`. | 1-2 | Locate the task through Codex indexes/local task storage before relying on reconstructed repository history. |
| Windows PowerShell does not support `Invoke-WebRequest -SkipHttpErrorCheck` in this environment. | 1 | Switched endpoint probing to `curl.exe` with status-only output. |
| A parallel status probe aborted on the first unreachable URL. | 1 | Re-ran with `Promise.allSettled` so healthy and unavailable services were recorded independently. |
| PowerShell 5 rejected piping a bare `foreach` expression in the OpenAPI probe. | 1 | Wrapped the loop in an array expression before JSON serialization. |
| Initial sub2api source search included nonexistent top-level `internal` and `cmd` paths and returned exit 1 after useful matches. | 1 | Restricted subsequent searches to the actual `backend/internal` and `frontend` source roots. |
| Tried to read nonexistent sub2api route file `backend/internal/server/routes/public.go`. | 1 | Use the actual split route files such as `auth.go` and `admin.go`. |
| A JavaScript orchestration call referenced `commands` after declaring the array as `calls`. | 1 | Corrected the local variable name and reran the read-only source inspection. |
| The current sub2api container logs no longer include the one-time generated administrator password. | 1 | Used the existing admin API key from the local database only in process memory; no secret was printed or persisted by the probe. |
| A combined artifact check returned exit 1 because one optional not-yet-created test path was passed to `Get-ChildItem`. | 1 | Re-ran status/diff checks independently and inspected only files that existed. |
| Second targeted run found the same obsolete `$100` monthly expectation for a future subscription type. | 2 | Updated the future-type assertion to the derived `$400` fallback boundary. |
| Playwright wrapper failed under Windows because `bash` resolved to WSL, first losing the Windows path and then rejecting the CRLF script. | 1-2 | Used the wrapper's documented underlying `npx --package @playwright/cli playwright-cli` command directly; CLI availability verified. |
| First mobile navigation click used a stale desktop ref after viewport resize. | 1 | Captured a fresh mobile snapshot and switched to the current navigation ref before continuing. |
| Adding `min-width: 0` to the sample panels alone did not contain the 1030px table; document width remained 1005px. | 1 | Inspect the exact overflowing DOM ancestors/computed grid sizes before the second CSS attempt. |
| Full backend suite found a runtime-settings persistence test still expecting an explicitly supplied non-Team monthly upper bound of `$1000`. | 1 | Updated persistence expectations to prove backend normalization ignores stale monthly input and stores weekly×4 (`$1200-$1600`). |
| First deployment bundle used only a raw revision range, leaving no advertised ref and producing an empty bundle. | 1 | Recreated it with named `main` plus excluded base `^5151aa7`; `git bundle verify` succeeded with x1's current commit as prerequisite. |
| First combined x1 preflight/backup command again hit nested zsh quoting around command substitution. | 1 | Moved Git preflight and SQLite backup entirely into the base64 Python script, leaving the SSH shell command quote-free. |
| First backend-subtask planning patch targeted a heading that exists only in `progress.md`, not `task_plan.md`. | 1 | Located the exact task-plan heading and applied a scoped checklist there. |
| PowerShell/rg rejected the literal path pattern `backend/test*.py`. | 1 | Use `rg -g 'test*.py' backend` or direct file paths instead of shell wildcard expansion. |
| Sibling sub2api source had no text match for the new payment-balance recharge setting. | 1 | Implement the required `/admin/settings` client contract with envelope-aware parsing and strict missing-field-only default semantics. |
| A PowerShell `rg` command used embedded escaped double quotes and failed parsing. | 1 | Use single-quoted PowerShell regex arguments for route/source searches. |
| First focused backend run had one contradictory test assertion that the upstream adapter call both omitted and contained `api_key`. | 1 | Removed only the incorrect omission assertion; the adapter must receive the decrypted key in memory while public output/storage tests enforce non-disclosure. |
| Final parallel verification stopped early because the expected no-match secret scan returned `rg` exit code 1, hiding the concurrent test output. | 1 | Make no-match an explicit successful security-scan result and rerun the full suite separately. |
| First PowerShell security scan treated safe `detail=exc.public_message` as unsafe because its regex matched every `detail=exc...` prefix. | 1 | Refine the pattern to flag only direct exception-object/string mappings; retain the explicit public-message mapping. |
## Sample Page Window Grouping (2026-07-13)

- [x] Phase 1: Trace the sample API window classification and current sample-page rendering.
- [x] Phase 2: Group visible samples by 5h, 7d, and monthly windows; exclude non-monthly accounts from the monthly group.
- [x] Phase 3: Hide groups without samples and add focused regression coverage.
- [x] Phase 4: Run backend/frontend verification and inspect the samples page at desktop and mobile sizes.

### Acceptance Criteria

- The samples page presents samples under 5h, 7d, and monthly window groups in that order.
- The monthly group counts only accounts that actually have a monthly quota window.
- Any window group with no persisted samples is omitted from the page.

### Errors Encountered

- The first planning-file update patch omitted valid hunk context before the next file marker, so `apply_patch` rejected it without changing files. Reissued the patch with complete context and it applied cleanly.
- The first targeted backend test command ran from the repository root, so imports of the `app` package failed. Re-run from the `backend` directory, matching the project's test layout.
- The first Playwright `run-code` call supplied a statement block instead of the CLI-required page function; the corrected function then exposed that the isolated runner lacks a global `URL` constructor. Switched the mock router to URL suffix matching and clear old handlers before retrying.

### Final Verification

- Frontend production build passed with Vite 6.4.3.
- 34 focused usage-estimate and usage-limit sample API tests passed.
- `git diff --check` passed.
- Playwright verified `5h`, `7d`, and `月` tab order and aggregate counts, true-monthly-only rendering, hidden empty cohort panels, hidden empty window tabs, and 390px document containment.

## Account Identity Across Dashboard and Usage Views (2026-07-13)

- [x] Phase 1: Trace the current recent-account, rate-limit-account, and usage-detail render/data paths without disturbing concurrent upstream-management work.
- [x] Phase 2: Expose account names in usage-estimate rows and add shared compact/stacked identity renderers.
- [x] Phase 3: Apply `名称 | 邮箱` to long compact rows and stacked copyable identity to account-detail tables.
- [x] Phase 4: Verify backend/frontend regressions plus desktop/mobile layouts.

### Acceptance Criteria

- Overview recent accounts show account name and email on one compact line separated by `|`.
- Overview rate-limited account rows show account name and email in the same compact format.
- Usage account details show account name above email, with both values independently copyable.
- Missing names fall back to email without blank identity content.
- Existing uncommitted upstream-management and sample-grouping changes are preserved.

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| First planning append targeted a progress line that had changed in the concurrent sample-grouping work. | 1 | Read the actual tails of all three planning files and append against their current final lines. |
| First parallel symbol search used a PowerShell-escaped regex that `rg` parsed as an unclosed group. | 1 | Switched to single-quoted focused patterns and tolerated optional no-match searches explicitly. |
| First estimator-test symbol search stopped on expected no-match results. | 1 | Reran with explicit no-match success handling and confirmed no existing direct `_account_estimate` test needs fixture updates. |
| Playwright's broad `**/api/**` fallback route intercepted specific fixture routes registered later, leaving the app on login. | 1 | Remove the broad route and register only the exact empty-list endpoints needed by initial loading. |

### Final Verification

- Overview recent-account and rate-limit rows render copyable `name | email` identities without overlapping status/recovery badges.
- The usage-detail table header is `账号`; normal and rate-limited rows render a copyable stacked name and email.
- Clipboard reads confirmed that name and email buttons copy distinct values in both compact and stacked treatments.
- Desktop and 390px mobile Playwright checks passed with no document-level horizontal overflow; the wide usage table remains contained in its own horizontal scroller.
- 36 focused backend tests, the frontend production build, and `git diff --check` passed.

## Change Request: Upstream Channel Cards and Balance Repair (2026-07-13)

- [x] Phase 1: Inspect `upstream-ops` channel/card UX, current persistence/API boundaries, and live NewAPI/Sub2API response contracts.
- [x] Phase 2: Introduce normalized upstream-channel persistence keyed by base URL with trailing `/v1` ignored, plus a compatibility migration from per-account configuration.
- [x] Phase 3: Move protocol, Access Token, recharge discovery, and site balance probing to channels; repair NewAPI balance parsing and keep account-specific group/rate state below each channel.
- [x] Phase 4: Replace the account table with operational channel cards and nested API-key account rows, including NewAPI/Sub2API credential guidance.
- [x] Phase 5: Use the authorized logged-in browser sessions for visible credential setup where supported, then test with 章鱼哥 (NewAPI) and 哈基米 (Sub2API) without reading browser storage or exposing secrets.
- [x] Phase 6: Run focused/full tests, security and secret scans, production build, and responsive-layout verification against the isolated preview.

### Acceptance Criteria

- URLs that differ only by a trailing `/v1` are grouped into one upstream channel.
- Channel cards own the upstream protocol, Access Token configuration, recharge cost, probe state, and one site-level balance display.
- Nested account cards retain account identity, selected group multiplier, current/target sub2api multiplier, and explicit apply controls.
- NewAPI balance discovery uses the correct authenticated user/quota fields and is verified against the 章鱼哥 channel.
- Sub2API channel discovery is verified against 哈基米.
- Full tokens never appear in API responses, browser text, logs, screenshots, or Git diffs; all state-changing multiplier writes remain explicit and confirmed.

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| Claiming the existing `zz1cc.cc.cd/wallet` Chrome tab and immediately requesting a full DOM snapshot timed out and reset the browser-control session. | 1 | Reconnect, claim one tab at a time, and begin with URL/title or a small visible-DOM read before requesting a snapshot. |
| Claiming the same ZZ Chrome tab and requesting only URL/title still timed out and reset the browser connection. | 2 | Read Chrome connection troubleshooting, then try a fresh controlled tab or visible navigation without inspecting browser storage. |
| The local `output/upstream-ops-ref` worktree has every tracked file deleted while Git objects remain available. | 1 | Preserve the dirty reference worktree and inspect source with `git show HEAD:path` instead of restoring files. |
## Commit and Deploy Sample Grouping to x1 (2026-07-13)

- [x] Phase 1: Isolate the sample-page grouping diff from unrelated local work and re-run focused verification.
- [x] Phase 2: Stage only the sample grouping hunks and create one scoped conventional commit.
- [x] Phase 3: Preflight x1 revision/worktree/services and create a rollback-ready repository backup state.
- [x] Phase 4: Transfer and fast-forward the isolated commit, build the frontend, restart services, and verify production behavior.

### Acceptance Criteria

- The commit contains only the sample-page 5h/7d/month grouping and empty-group hiding change.
- Existing uncommitted API-key, identity, backend, and planning changes remain uncommitted and intact locally.
- x1 reaches the new commit without overwriting unrelated remote worktree files.
- x1 frontend and backend are healthy after deployment, and the authenticated sample API/page remain available.

### Errors Encountered

- `git diff -G` selected the entire modified `App.tsx` file rather than only matching hunks. Use Git's hunk staging and inspect the staged diff before committing.
- Replaying the staged multi-hunk patch into a temporary worktree with plain `git apply` failed on the second offset-sensitive hunk; the temporary worktree was cleaned automatically and the real index was unchanged. Commit the verified index first, then build a clean worktree from the resulting commit tree.
- The first post-commit planning update patch omitted valid hunk context before the next file marker, so `apply_patch` rejected it without changing files. Reissued with complete context.
- The first pre-deployment planning update repeated the same missing-context patch shape and was rejected without changes. Reissued with explicit context and retained as a workflow error.

### Final Verification

- Local commit: `885e603 feat(samples): group quota samples by window`.
- Local and x1 clean-tree frontend installs/audits/builds passed; audits reported zero vulnerabilities.
- x1 `main` fast-forwarded from `504a977` to `885e603`; only the pre-existing untracked `01-login.png` remains.
- x1 frontend/backend services are active, frontend returns HTTP 200, and backend health returns OK.
- Authenticated production login and sample API both return HTTP 200; current visible sample counts are 5h=157, 7d=39, monthly=14.
- Temporary local/remote bundles, deploy ref, and temporary worktrees were removed; local unrelated changes remain intact and unstaged.

## Commit and Deploy Account Identity Views to x1 (2026-07-13)

- [x] Phase 1: Isolate only the recent-account, rate-limit-account, and usage-detail identity code/tests from unrelated upstream-channel work.
- [x] Phase 2: Stage the isolated patch, verify the staged tree in a clean worktree, and create one scoped conventional commit.
- [x] Phase 3: Preflight x1 revision, tracked worktree state, services, and rollback readiness.
- [x] Phase 4: Transfer and fast-forward the verified commit, build/restart the affected services, and verify production health and identity API fields.

### Acceptance Criteria

- The commit contains only the account-name/email display contract, UI renderers/styles, and focused regression test.
- Existing upstream-channel implementation and planning changes remain unstaged and intact locally.
- x1 fast-forwards from its exact clean tracked base without removing the pre-existing untracked screenshot.
- x1 backend/frontend are healthy after deployment, and production usage-estimate rows expose `account_name` separately from `email`.

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| The first final planning update targeted an error-row context that concurrent planning edits had moved into an earlier section. | 1 | Re-read the identity section and applied the completion update against its current empty error table. |

### Final Verification

- Local commit: `c8f1fbe feat(accounts): show account names across dashboard views` (six identity-only files).
- Clean-tree local and x1 verification passed 47 backend tests, frontend production builds, and zero-vulnerability npm audits.
- x1 fast-forwarded from `885e603` to `c8f1fbe`; both services are active and backend health/frontend/authenticated usage endpoints return HTTP 200.
- All 60 production usage rows expose `account_name` and `email`; 58 currently contain distinct values.
- Compiled production assets contain both copy labels and the compact identity/badge selectors; x1 still preserves only `01-login.png` as an untracked file.
- Temporary local/remote bundle, verification worktree, pytest target, and deploy ref were removed; the timestamped x1 rollback ref remains.
## Sample Subscription Secondary Navigation (2026-07-13)

- [x] Phase 1: Confirm the existing grouped sample data/state and define the two-level navigation behavior.
- [x] Phase 2: Add window-level sample/subscription counts and subscription-level navigation.
- [x] Phase 2b: Add non-empty 5h/7d/month filtering to the rate-limited accounts panel.
- [x] Phase 3: Build and verify state switching, empty-group behavior, and desktop/mobile layout.

### Acceptance Criteria

- Each visible 5h/7d/month primary tab shows total samples and the number of non-empty subscription types.
- The selected window exposes a secondary tab for each non-empty subscription type, including that subscription's sample count.
- Switching the primary window selects a valid subscription automatically; switching the secondary tab shows only that subscription's calibration and table.
- Existing empty-window and empty-subscription hiding behavior remains unchanged.
- The rate-limited accounts panel shows a filter only for windows that currently contain rate-limited accounts and renders only the selected window list.

### Final Verification

- TypeScript/Vite production build passed and `git diff --check` passed.
- Playwright verified primary sample/subscription counts, subscription secondary selection, automatic valid selection after window changes, and single-panel rendering.
- Playwright verified rate-limit filters hide the empty 7d window and switch between 5h/month account lists.
- Desktop interaction and 390px mobile containment passed; browser errors were only expected fixture 401 responses for the intentionally unmocked usage-estimate endpoint.
## Commit and Deploy Secondary Navigation to x1 (2026-07-13)

- [x] Phase 1: Recreate only the two navigation changes in a clean worktree based on the current shared x1/local revision.
- [x] Phase 2: Build, inspect, and commit the isolated frontend diff with a scoped conventional message.
- [x] Phase 3: Advance local main without disturbing the dirty worktree, then bundle/preflight/deploy to x1.
- [x] Phase 4: Verify production build, services, authenticated sample data, and clean temporary deployment artifacts.

### Acceptance Criteria

- The commit contains only the sample secondary navigation/counts and non-empty rate-limit window filters.
- Existing API Key/backend/planning changes and the pre-existing identity verification worktree remain untouched.
- x1 fast-forwards from `c8f1fbe` to the new commit and both services remain healthy.

### Final Verification

- Commit: `166ed56 feat(quota): add secondary window filters`.
- Clean local/x1 installs, zero-vulnerability audits, TypeScript checks, and Vite production builds passed.
- x1 fast-forwarded from `c8f1fbe` to `166ed56`; frontend/backend services are active, frontend returns HTTP 200, and backend health is OK.
- Authenticated production APIs return 200. Sample primary metrics are 5h=163/3 subscriptions, 7d=40/3, monthly=14/1.
- Production rate-limit counts are 5h=5, 7d=15, monthly=0, so the monthly filter is correctly omitted under the new non-empty rule.
- Temporary local/remote bundles, deploy ref, branch, and task-created worktree were removed; unrelated local changes remain unstaged.
## Usage Detail Rate-Limit Window Filters (2026-07-13)

- [x] Phase 1: Trace account-detail filters and normalize 5h/7d/month rate-limit membership.
- [x] Phase 2: Add non-empty window buttons to the left side of the account quota detail header.
- [x] Phase 3: Build and verify interaction order with subscription filters on desktop/mobile.

### Acceptance Criteria

- Window buttons appear only while “限流账号” is selected in “账号额度明细”.
- Only 5h/7d/month windows with at least one visible rate-limited detail account are shown.
- Selecting a window filters detail rows before subscription filtering and clears a stale subscription selection.
- Monthly-backed five-hour/seven-day rate-limit signals normalize to the monthly button.

### Final Verification

- TypeScript/Vite production build and `git diff --check` passed.
- Playwright verified normal accounts hide the window tabs, while rate-limited accounts show only the non-empty `5h 2` and `month 2` tabs and omit 7d.
- The 5h/month row sets switch correctly, changing windows clears the prior subscription selection, and monthly `window_kind` normalization is covered.
- At 390px, document/body scroll width remains equal to the viewport; the browser console has zero errors and warnings.

## Commit and Deploy Usage Detail Filters to x1 (2026-07-13)

- [x] Phase 1: Recreate only the usage-detail rate-limit window filter in a clean worktree and verify its build/diff.
- [x] Phase 2: Stage the verified patch exactly, create a scoped conventional commit, and preserve unrelated local changes.
- [x] Phase 3: Preflight x1 revision, tracked worktree state, services, disk space, and rollback readiness.
- [x] Phase 4: Transfer, remotely verify, fast-forward, rebuild/restart, and confirm production health and cleanup.

### Acceptance Criteria

- The commit contains only the account quota detail 5h/7d/month filter behavior and its small layout rule.
- Local API Key/backend/planning work remains unstaged and intact.
- x1 advances from the exact expected clean tracked base and preserves its pre-existing untracked screenshot.
- The x1 frontend/backend services and HTTP health checks pass after deployment.

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| PowerShell's text pipeline rewrote the clean-worktree patch stream, so `git apply --cached` could not match CRLF-aware index lines. No hunks were staged. | 1 | Verify the index remains empty, then use a native `cmd.exe` byte-preserving pipe for the exact same verified patch. |
| The post-deploy exact Chinese-label grep returned no match in the minified JS and made the otherwise successful verification script exit 1 after health checks passed. | 1 | Re-run final readback using the stable ASCII class name in source/compiled assets, then clean temporary deployment refs/files. |

### Final Verification

- Commit: `f8c48df feat(quota): filter rate-limited usage details by window` with only `frontend/src/App.tsx` and `frontend/src/styles.css`.
- Local and x1 clean-worktree dependency installs, zero-vulnerability audits, TypeScript checks, and Vite production builds passed.
- x1 fast-forwarded from `166ed56` to `f8c48df`; both services are active, backend health is OK, and frontend returns HTTP 200.
- Source and compiled assets contain the usage-detail filter selector; x1 preserves only `01-login.png` as untracked.
- Temporary local/remote bundles, deployment ref, and verification worktrees were removed; the timestamped x1 rollback ref remains.

## Sub2API Refresh Token Persistence (2026-07-13)

- [x] Phase 1: Verify the local Sub2API refresh endpoint contract and token rotation behavior.
- [x] Phase 2: Add encrypted channel-level Refresh Token storage with an idempotent SQLite migration and redacted API contract.
- [x] Phase 3: Refresh expired Sub2API Access Tokens, persist rotated AT/RT atomically, and retry discovery once.
- [x] Phase 4: Add a non-echoing Sub2API Refresh Token field, configured-state indicator, and clear action to the channel UI.
- [x] Phase 5: Capture the signed-in Hakimi AT/RT through a visible login response, then verify balance, groups, rotation, builds, tests, and secret-safe logs.

### Acceptance Criteria

- Refresh Tokens are encrypted at rest and never appear in API responses, validation errors, application logs, or frontend state after save.
- A Sub2API channel with an expired Access Token can use its saved Refresh Token to obtain and atomically store the rotated token pair before one discovery retry.
- Failed refresh attempts preserve the previously saved AT/RT pair and return only a generic credential error.
- NewAPI discovery is unchanged and never sends a Sub2API Refresh Token.
- The Hakimi channel still reports its real balance and group multipliers after the persisted Refresh Token flow is enabled.

### Final Verification

- Local Sub2API source confirmed a 24-hour AT, 30-day rotating RT, required JSON request, and standard token-pair response envelope.
- Hakimi live forced-401 rotation succeeded, then survived a preview-backend restart with balance `$45.0821`, 11 groups, recharge cost `1 CNY/USD`, and both credential flags set.
- Full backend regression passed `139/139`; frontend production build, zero-vulnerability npm audit, compileall, diff check, and preview-log secret-pattern scan passed.
- Desktop and 390px browser checks passed with no app-origin console errors, no horizontal overflow, and no token value rendered in the channel card or configuration dialog.

## Upstream API Key Group Synchronization (2026-07-13)

- [x] Phase 1: Trace channel discovery, encrypted account API Key storage, and upstream Key/group response formats.
- [x] Phase 2: Match all bound account API Keys during one channel discovery and persist only unambiguous upstream group matches.
- [x] Phase 3: Surface the synchronized group source and clarify that NewAPI usage is the upstream account's cumulative usage.
- [x] Phase 4: Add secret-safe regression coverage and verify Hakimi/Octopus in the local preview.

### Acceptance Criteria

- One channel discovery fetches upstream data once and can match multiple bound API Key accounts to different groups.
- Matching uses the complete local Key only in memory and prefers an exact revealed value; a masked-list fallback requires a fully anchored, bidirectionally unique match.
- Keys, ciphertext, and matching fragments never enter API responses, logs, screenshots, or persisted plaintext.
- A successful exact match updates the account's selected group and recalculates its derived billing multiplier.
- Missing, ambiguous, or unmatched Keys preserve the account's prior manual group instead of clearing or guessing it.
- The channel usage label states that NewAPI `used_quota`/`quota_used` is cumulative upstream account usage.

### Errors Encountered

| Error | Attempt | Resolution |
|---|---:|---|
| PowerShell treated the missing `.venv\\Scripts\\python.exe` path as a module, then the system Python 3.12 lacked backend dependencies. | 1 | Reused the running preview backend's `C:\\Python314\\python.exe`; all checks then ran in the correct dependency environment. |
| A diagnostic script selected the Chinese channel name through a non-UTF-8 PowerShell pipe and found no row. | 1 | Selected the channel by its NewAPI type and six-account structure; no upstream request was sent by the failed attempt. |
| Repeated live reveal diagnostics triggered NewAPI HTTP 429 responses. | 1 | Kept reveal as the preferred exact path and added the strict, token-ID-deduplicated mask fallback for rate-limited or inconsistent reveal values. |

### Final Verification

- Full backend regression passed `143/143`; frontend TypeScript/Vite production build and `git diff --check` passed.
- Hakimi synchronized `5/5` accounts through exact full-Key matching; target rates are `0.04`, `0.04`, `0.11`, `0.11`, and `0.02`.
- Octopus synchronized `6/6` accounts. Four current groups produced target rates `0.1739`, `0.2049`, `0.3043`, and `0.2049`; two retired groups retained their real names with `group_rate_unavailable` and no guessed rate.
- Octopus balance is `$502.1853`; `used_quota / quota_per_unit` is shown as `上游累计已用 $10,115.5108`.
- Desktop and 390px browser checks passed without page-level overflow or overlapping account controls; a clean reload produced no new console warnings or errors.
- Preview backend is healthy on port `8001`, and the API Key page remains available at `http://127.0.0.1:5174/`. No sub2api billing multiplier was applied.

## Automatic API Key Billing Synchronization and Audit Log (2026-07-13)

- [x] Phase 1: Audit the current calculation, local recharge setting, account operations, settings UI, and Cheese credential availability.
- [ ] Phase 2: Persist exported API Keys encrypted, add automatic-sync/log-retention settings, and add a billing-change log model/API.
- [ ] Phase 3: Calculate `target = upstream group x upstream recharge cost / local recharge cost` and automatically apply safe changes when enabled.
- [ ] Phase 4: Replace unmanage/apply actions with disable/delete account operations and simplify manual group fallback configuration.
- [ ] Phase 5: Add cached-first loading, collapsible groups, channel/account card layouts, and management/log sub-navigation.
- [ ] Phase 6: Separate OAuth inspection and usage settings, verify whether usage switches overlap, and add new API billing settings.
- [ ] Phase 7: Acquire and validate Cheese credentials, then verify Cheese, Octopus, and Hakimi end to end.

### Acceptance Criteria

- API Keys imported from the protected local sub2api export are encrypted before persistence and never appear in API responses, logs, screenshots, validation errors, or plaintext database storage.
- Automatic rate application is disabled by default, admin-configurable, auditable, and only writes a finite positive target derived from `G x U / L` after a successful unambiguous group match.
- Every attempted rate change records account/channel identity, old/new rates, input multipliers, status, and a generic error without storing credentials.
- Disabling and deleting an API Key account require authenticated endpoints; deletion requires an explicit confirmation payload and removes the actual sub2api account plus local configuration.
- Manual group name selection is removed. The upstream group name/rate is authoritative; a manual group multiplier is available only when the upstream rate cannot be resolved.
- Cached account/channel data renders immediately on page re-entry, followed by a background refresh that cannot blank the previous view.
- Channel groups can expand/collapse; each channel is one card and each API Key account is a compact child card with title, group, rate, and actions.
- The API Key page has Management and Rate Changes tabs; retention is configurable and expired log rows are pruned.
- Cheese balance, recharge cost, and per-Key group matching are verified without reading browser Cookie/Local Storage or exposing credentials.
