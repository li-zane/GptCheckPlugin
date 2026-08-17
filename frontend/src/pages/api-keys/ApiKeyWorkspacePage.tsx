import { Suspense } from "react";

import { ApiKeyAccountsView } from "../../features/api-accounts/ApiKeyWorkspace";
import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import type { ApiKeySubview } from "../../viewRouting";
import styles from "./ApiKeysPage.module.css";

export function ApiKeyWorkspacePage({ subview }: { subview: ApiKeySubview }) {
  const props = useDashboardPage("apiKeys");
  return (
    <Suspense fallback={<div className="empty-state" role="status">正在加载 API Key 页面</div>}>
      <div className={styles.page}><ApiKeyAccountsView {...props} subview={subview} /></div>
    </Suspense>
  );
}
