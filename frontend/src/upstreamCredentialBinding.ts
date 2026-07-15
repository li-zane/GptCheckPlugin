import type { UpstreamChannel } from "./types";

export function channelCredentialBindingChanged(
  channel: UpstreamChannel,
  nextCanonicalUrl: string,
  nextManagementUrl: string,
) {
  const previousCanonicalUrl = channel.canonical_base_url?.trim() || channel.base_url?.trim() || "";
  const previousManagementUrl = channel.management_base_url?.trim() || previousCanonicalUrl;
  const effectiveNextManagementUrl = nextManagementUrl.trim() || nextCanonicalUrl;
  return (
    credentialOrigin(previousCanonicalUrl) !== credentialOrigin(nextCanonicalUrl) ||
    credentialOrigin(previousManagementUrl) !== credentialOrigin(effectiveNextManagementUrl)
  );
}

function credentialOrigin(value: string) {
  try {
    return new URL(value).origin.toLowerCase();
  } catch {
    return value.trim().toLowerCase();
  }
}
