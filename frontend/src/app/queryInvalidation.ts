import type { QueryClient, QueryKey } from "@tanstack/react-query";

import { clearChangeLogCache } from "../changeLogCache.ts";
import { clearUpstreamOverviewCache } from "../upstreamOverviewCache.ts";
import { mutationInvalidationMatrix, type MutationDomain } from "./queryKeys.ts";

export async function invalidateMutationDomain(queryClient: QueryClient, domain: MutationDomain) {
  await Promise.all(
    mutationInvalidationMatrix[domain].map((queryKey) =>
      queryClient.invalidateQueries({ queryKey: queryKey as QueryKey }),
    ),
  );
}

export async function clearManagementSiteQueries(queryClient: QueryClient) {
  await Promise.all([
    queryClient.cancelQueries({ queryKey: ["upstreams"] }),
    queryClient.cancelQueries({ queryKey: ["usage"] }),
    queryClient.cancelQueries({ queryKey: ["changeLogs"] }),
  ]);
  queryClient.removeQueries({ queryKey: ["upstreams"] });
  queryClient.removeQueries({ queryKey: ["usage"] });
  queryClient.removeQueries({ queryKey: ["changeLogs"] });
}

export function clearSessionStorageCaches(storage: Storage = sessionStorage) {
  clearUpstreamOverviewCache(storage);
  clearChangeLogCache(storage);
}
