import type { ReactNode } from "react";

import styles from "./EmptyState.module.css";

export function EmptyState({ title, description, action }: { title: ReactNode; description?: ReactNode; action?: ReactNode }) {
  return <div className={styles.empty}><strong>{title}</strong>{description ? <p>{description}</p> : null}{action}</div>;
}
