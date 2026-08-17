import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import { UsageEstimateView } from "../../features/usage/UsageEstimateView";
import styles from "./UsagePage.module.css";

export default function UsagePage() {
  return <div className={styles.page}><UsageEstimateView {...useDashboardPage("usage")} /></div>;
}
