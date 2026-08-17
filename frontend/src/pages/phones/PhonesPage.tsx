import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import { PhoneView } from "../../features/phones/PhoneView";
import styles from "./PhonesPage.module.css";

export default function PhonesPage() {
  return <div className={styles.page}><PhoneView {...useDashboardPage("phones")} /></div>;
}
