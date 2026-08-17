import type { Upstream } from "./domain";

export function upstreamCredentialBindingChanged(
  upstream: Upstream,
  nextCanonicalUrl: string,
  nextManagementUrl: string,
) {
  const previousCanonicalUrl = upstream.api_endpoint_url?.trim() || "";
  const previousManagementUrl = upstream.management_url?.trim() || previousCanonicalUrl;
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
