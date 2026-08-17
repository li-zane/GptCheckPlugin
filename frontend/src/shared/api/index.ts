export { AUTH_EXPIRED_EVENT, ApiError, NO_FRONTEND_TIMEOUT, request } from "./client.ts";
export { authApi } from "./auth.ts";
export { accountsApi } from "./accounts.ts";
export { mailboxesApi } from "./mailboxes.ts";
export { phonesApi } from "./phones.ts";
export { settingsApi } from "./settings.ts";
export {
  upstreamChangeLogsPath,
  upstreamLegacyBindingCounts,
  upstreamLegacyIdentityBindings,
  upstreamUsageHistoryPath,
  upstreamsApi,
} from "./upstreams.ts";

import { accountsApi } from "./accounts.ts";
import { authApi } from "./auth.ts";
import { mailboxesApi } from "./mailboxes.ts";
import { phonesApi } from "./phones.ts";
import { settingsApi } from "./settings.ts";
import { upstreamsApi } from "./upstreams.ts";

// Temporary aggregate while the remaining legacy feature components migrate.
export const api = {
  ...authApi,
  ...accountsApi,
  ...settingsApi,
  ...mailboxesApi,
  ...phonesApi,
  ...upstreamsApi,
};
