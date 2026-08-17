import { HistoryView } from "../../HistoryView";
import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import styles from "./HistoryPage.module.css";

export default function HistoryPage() {
  return <div className={styles.page}><HistoryView {...useDashboardPage("history")} /></div>;
}
