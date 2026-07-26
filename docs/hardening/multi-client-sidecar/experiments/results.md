# Local Feasibility Experiment Results

Date: 2026-07-18 (Asia/Shanghai)

These experiments use synthetic credentials only. They do not connect to x1,
x2, a live sub2api API, or a live sub2api database.

## Authenticated Encryption Probe

Command:

```powershell
.\.venv\Scripts\python.exe docs\hardening\multi-client-sidecar\experiments\protocol_probe.py
```

Environment:

- Python 3.14.3 on Windows
- cryptography 49.0.0
- X25519, HKDF-SHA256, ChaCha20-Poly1305, and Ed25519 primitives

All eight checks passed:

- valid round trip;
- ciphertext tampering rejection;
- authenticated metadata tampering rejection;
- replay rejection;
- expired-message rejection;
- wrong-receiver decryption rejection;
- cross-instance response rejection;
- oversized batch rejection.

One benchmark run sealed, signed, verified, and opened 500 envelopes with 100
synthetic API Key records per envelope:

| Metric | Measured result |
| --- | ---: |
| Median round-trip latency | 1.4012 ms |
| P95 round-trip latency | 2.3576 ms |
| Throughput | 605.24 envelopes/s |
| Median encoded envelope | 13,372 bytes |
| Python heap peak during benchmark | 230,868 bytes |
| Process RSS before/after | 30,146,560 / 30,212,096 bytes |
| Process peak RSS | 30,666,752 bytes |

This measures in-process envelope cryptography only. It excludes TLS,
WebSocket, database, container, and network overhead. Python process memory is
not a forecast for a production Go connector.

## PostgreSQL Least-Privilege Probe

Command:

```powershell
powershell -ExecutionPolicy Bypass -File .\docs\hardening\multi-client-sidecar\experiments\readonly_probe.ps1
```

The script started an unpublished `postgres:18-alpine` container, loaded only
synthetic rows, created a non-superuser connector role, and removed the
container on completion.

Allowed checks:

- select the API Key inspection view;
- select the OAuth inspection-input view;
- observe the read-only transaction default;
- observe the three-second statement timeout.

Denied checks:

- select the base accounts table;
- select the administrator secrets table;
- select an OAuth refresh-token column that the view does not expose;
- update the API Key view;
- update the base table after overriding the read-only session default;
- create a table in the public schema;
- call `pg_read_file`.

The override test is important: `default_transaction_read_only=on` is only
defense in depth. The narrow grants and views remained the actual authorization
boundary when the session default was changed.
