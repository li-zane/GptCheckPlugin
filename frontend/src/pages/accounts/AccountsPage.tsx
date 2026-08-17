import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import { AccountsView } from "../../features/accounts/AccountsView";
import styles from "./AccountsPage.module.css";

export default function AccountsPage() {
  return <div className={styles.page}><AccountsView {...useDashboardPage("accounts")} /></div>;
}
