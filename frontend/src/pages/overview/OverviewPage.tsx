import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import { Overview } from "../../features/overview/OverviewView";
import styles from "./OverviewPage.module.css";

export default function OverviewPage() {
  return <div className={styles.page}><Overview {...useDashboardPage("overview")} /></div>;
}
