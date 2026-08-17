import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import { UsageLimitSamplesView } from "../../features/usage-samples/UsageLimitSamplesView";
import styles from "./UsageSamplesPage.module.css";

export default function UsageSamplesPage() {
  return <div className={styles.page}><UsageLimitSamplesView {...useDashboardPage("usageSamples")} /></div>;
}
