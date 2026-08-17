import { useDashboardPage } from "../../app/dashboard/DashboardContext";
import { MailboxView } from "../../features/mailboxes/MailboxView";
import styles from "./MailboxesPage.module.css";

export default function MailboxesPage() {
  return <div className={styles.page}><MailboxView {...useDashboardPage("mailboxes")} /></div>;
}
