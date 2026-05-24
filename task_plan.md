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
