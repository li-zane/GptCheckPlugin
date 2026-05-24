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
