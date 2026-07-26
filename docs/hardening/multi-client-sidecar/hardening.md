# Security Hardening Review: Multi-Instance sub2api Connector

## Evidence Basis

This review covers the inspected GptCheckPlugin working tree at repository HEAD
`a5e99cf293f4ea1a0343e32f95ea27789b3d2a9b`, with pre-existing working-tree
drift recorded in [`context.md`](context.md). It also uses read-only x2 runtime
and schema observations plus two synthetic local experiments. No x1/x2
deployment or live credential was changed for this analysis.

The important evidence is structural: the current control plane, database
identity, schedulers, UI, and cache assume one sub2api instance; x2 stores raw
credential fields in PostgreSQL JSONB; and the protected export endpoint is an
intentional step-up boundary. A direct database connector is feasible, but it
must be treated as a new privileged secret reader rather than a TOTP workaround.

## Constraints

We assume a balanced security and operability profile. Existing single-instance
data must migrate without loss, normal list/rename/enable/disable/refresh work
must continue through per-instance x-api-key, and no TOTP seed or administrator
session may be stored. The connector should be outbound-only, non-root, and
small; unknown sub2api schemas must fail closed. Raw OAuth RT is outside the
normal capability set, and OAuth metadata should be derived locally without
sending AT/RT to x1.

## Opportunity Portfolio

| Opportunity | Evidence | Options | Recommendation | Proposal |
| --- | --- | --- | --- | --- |
| Establish instance identity and a least-authority sensitive-data boundary | Singleton configuration/identity, protected export dependency, raw JSONB credential storage, local protocol and PostgreSQL probes (`E01-E10`) | 1. API-only/manual; 2. paired read-only connector; 3. sub2api-owned integration | Option 2 now, with metadata-first capabilities and Option 1 as fallback; move to Option 3 if upstream ownership is available | [Complete proposal](proposals/multi-instance-sensitive-boundary.md) |

## Recommendation Summary

I recommend the paired connector only after multi-instance isolation is complete.
Every instance-owned row, API call, background job, cache entry, and UI response
must carry immutable `instance_id`; a logo selector cannot provide that boundary
by itself. The existing normal API client and the new sensitive provider should
be separate capabilities.

The connector should receive a narrow PostgreSQL role/view, initiate an outbound
mTLS connection, and return derived OAuth subscription metadata by default.
API Key group/rate inspection should also move local when practical. A raw API
Key compatibility response may be temporarily supported for selected account
IDs through an end-to-end sealed envelope. Raw OAuth RT should remain disabled
except for a separately designed break-glass process.

The connector itself should remain a small on-demand reader: one bounded DB
connection, one outbound session, no normal HTTP proxy, scheduler, full-account
cache, inbound port, host mount, or Docker Socket. Its installer may inspect an
explicit local candidate once, validate the sub2api schema/version, provision
narrow views, and then discard discovery privileges. An external read-only view
cannot prevent a compromised connector from enumerating rows visible to that
view; only an integration implemented inside sub2api can enforce stronger
request-level row authorization.

The local experiments support feasibility, not production readiness. Eight
protocol checks passed, and a synthetic 100-account envelope measured 1.4012 ms
median cryptographic round-trip latency. The PostgreSQL 18 probe allowed only
the two narrow views and denied base-table reads, hidden columns, writes, DDL,
and server-file reads. Full container, network, schema-version, leakage, and
target-host resource tests remain rollout gates.

## Next Decisions

The implementation should not start until two choices are explicit: whether x1
must ever receive raw API Keys, and whether a maintained sub2api integration API
is realistic. If raw Keys can stay local and sub2api cannot be patched, Option 2
has a clear, bounded MVP. If manual recovery is acceptable, Option 1 avoids the
new secret-reader process. If upstream accepts a derived-metadata integration,
Option 3 should become the long-term design.
