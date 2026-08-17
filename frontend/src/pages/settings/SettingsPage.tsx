import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import { SettingsView } from "../../features/settings/SettingsView";
import styles from "./SettingsPage.module.css";

export default function SettingsPage() {
  return <div className={styles.page}><SettingsView {...useDashboardPage("settings")} /></div>;
}
