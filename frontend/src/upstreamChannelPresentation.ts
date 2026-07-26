import type { UpstreamChannel } from "./types";

type UpstreamChannelAuthState = Pick<
  UpstreamChannel,
  "balance_status" | "channel_monitor_status" | "last_error"
>;

export function upstreamChannelTokenInvalid(channel: UpstreamChannelAuthState) {
  const statuses = [channel.balance_status, channel.channel_monitor_status]
    .map((value) => String(value || "").trim().toLowerCase());
  if (statuses.includes("token_invalid") || statuses.includes("credentials_rejected")) return true;
  return /token\s+(?:is\s+)?invalid|credentials\s+rejected/i.test(channel.last_error || "");
}

export function isGenericUpstreamChannelError(value?: string | null) {
  return /^(?:unable to read the upstream channel|upstream channel discovery failed)\.?$/i.test(
    String(value || "").trim(),
  );
}

export function partitionUpstreamChannels(channels: UpstreamChannel[]) {
  const assigned: UpstreamChannel[] = [];
  const enabled: UpstreamChannel[] = [];
  const noEnabled: UpstreamChannel[] = [];
  const empty: UpstreamChannel[] = [];

  for (const channel of channels) {
    const accounts = channel.accounts || [];
    const responseCount = Number(channel.account_count);
    const accountCount = Number.isFinite(responseCount)
      ? Math.max(0, Math.trunc(responseCount))
      : accounts.length;
    if (accountCount === 0) {
      empty.push(channel);
      continue;
    }

    assigned.push(channel);
    const completeAccountList = accounts.length >= accountCount;
    if (
      completeAccountList
      && accounts.length > 0
      && accounts.every((account) => account.remote_schedulable === false)
    ) {
      noEnabled.push(channel);
    } else {
      enabled.push(channel);
    }
  }

  return { assigned, enabled, noEnabled, empty };
}
