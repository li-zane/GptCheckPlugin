# Evidence Context: Multi-Instance sub2api Connector

## Scope And Identity

- Repository revision: `a5e99cf293f4ea1a0343e32f95ea27789b3d2a9b`
- Review date: 2026-07-18 (Asia/Shanghai)
- Source drift: present. The working tree contained pre-existing user changes,
  including changes to models, upstream services, and frontend files. This
  analysis describes the inspected working tree and records hashes for its
  principal evidence files; it does not claim the tree equals clean HEAD.
- Remote systems: x1 and x2 were inspected read-only. No deployment changes and
  no live credential exports were performed for this proposal.

## Evidence Inventory

| ID | Evidence | Identity | What it establishes |
| --- | --- | --- | --- |
| `E01` | `backend/app/services/runtime_config.py` | SHA-256 `be11b9689921f629fce6e8a3a6b6cb40910c2f0635baaa80c2a353deb9a01870` | One effective sub2api URL and x-api-key are stored and returned globally. |
| `E02` | `backend/app/services/sub2api.py` | SHA-256 `e671d4818bc1234c9a244fd53eb9755cb0401f142947229ef8648b6b66f616ed` | Many client methods fall back to the singleton runtime configuration; raw API Key export uses the TOTP-protected endpoint. |
| `E03` | `backend/app/models.py` | SHA-256 `a19a40117d0e2f26aa3b4832cbf2357f39ecbc5da17b897dadc5bb92a37ce273` | Account email, remote account ID, and upstream URL are globally unique rather than instance-scoped. |
| `E04` | `backend/app/services/upstream_rate_sync.py` | SHA-256 `d5311f27fec6f0466a1b27c7c5e07831f6c75dca8abe88a07d78f8a70addc4cf` | Inventory and upstream synchronization are global loops without per-instance work ownership. |
| `E05` | `frontend/src/App.tsx` | SHA-256 `cd2085ad0075d7e39348cafb0b7b2d464e1ce14d2ae14683c0429058f8d07e7a` | Settings and page loading assume one active URL; the logo area is the proposed selector location. |
| `E06` | `backend/app/core/crypto.py` | SHA-256 `62d3efdd524795a635991a9c348e623ce5ec8c3dacaf99f8b38956e61acffbfa` | Stored application secrets use one Fernet key derived from `APP_ENCRYPTION_KEY`; this is not a device pairing protocol. |
| `E07` | x2 runtime and schema observation | Read-only observation on 2026-07-18 | x2 runs sub2api with PostgreSQL 18 and Redis on an internal Docker network; `accounts.credentials` is JSONB containing raw credential fields. |
| `E08` | sub2api source/runtime version comparison | Read-only observation on 2026-07-18 | Inspected source was 0.1.136 while the runtime image reported 0.1.158, so a direct database adapter must detect schema/version drift and fail closed. |
| `E09` | `experiments/protocol_probe.py` and `experiments/results.md` | Local synthetic experiment on 2026-07-18 | Authenticated encryption, request binding, expiry, anti-replay, receiver binding, and batch limits are feasible with low cryptographic latency in a prototype. |
| `E10` | `experiments/readonly_probe_setup.sql`, `experiments/readonly_probe.ps1`, and `experiments/results.md` | Local synthetic PostgreSQL 18 experiment on 2026-07-18 | A dedicated role can read narrow views while base tables, hidden fields, writes, DDL, and server-file access remain denied. |

## Evidence Limitations

- x2 source and runtime versions differ. The live JSONB shape was inspected, but
  no compatibility claim is made for future sub2api releases.
- No live credential values were read into this repository or the experiments.
- The local protocol benchmark is not an end-to-end connector benchmark.
- A production Go toolchain was not present locally, so no Go binary RSS or CPU
  measurement was made.
- The review did not modify or test x1/x2 deployments.
