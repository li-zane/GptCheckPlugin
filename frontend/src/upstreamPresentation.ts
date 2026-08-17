import type { Upstream } from "./domain";

type UpstreamAuthState = Pick<
  Upstream,
  "balance_status" | "upstream_monitor_status" | "last_error"
>;

export function upstreamTokenInvalid(upstream: UpstreamAuthState) {
  const statuses = [upstream.balance_status, upstream.upstream_monitor_status]
    .map((value) => String(value || "").trim().toLowerCase());
  if (statuses.includes("token_invalid") || statuses.includes("credentials_rejected")) return true;
  return /token\s+(?:is\s+)?invalid|credentials\s+rejected/i.test(upstream.last_error || "");
}

export function isGenericUpstreamError(value?: string | null) {
  return /^(?:unable to read the upstream channel|upstream channel discovery failed)\.?$/i.test(
    String(value || "").trim(),
  );
}

export function partitionUpstreams(upstreams: Upstream[]) {
  const assigned: Upstream[] = [];
  const enabled: Upstream[] = [];
  const noEnabled: Upstream[] = [];
  const empty: Upstream[] = [];

  for (const upstream of upstreams) {
    const accounts = upstream.accounts || [];
    const responseCount = Number(upstream.account_count);
    const accountCount = Number.isFinite(responseCount)
      ? Math.max(0, Math.trunc(responseCount))
      : accounts.length;
    if (accountCount === 0) {
      empty.push(upstream);
      continue;
    }

    assigned.push(upstream);
    const completeAccountList = accounts.length >= accountCount;
    if (
      completeAccountList
      && accounts.length > 0
      && accounts.every((account) => account.remote_schedulable === false)
    ) {
      noEnabled.push(upstream);
    } else {
      enabled.push(upstream);
    }
  }

  return { assigned, enabled, noEnabled, empty };
}
