import type { HTMLAttributes, ReactNode } from "react";

import { cx } from "../../lib/cx";
import styles from "./Panel.module.css";

export function Panel({ className, title, tools, children, ...props }: HTMLAttributes<HTMLElement> & { title?: ReactNode; tools?: ReactNode }) {
  return (
    <section className={cx(styles.panel, className)} {...props}>
      {title || tools ? <header className={styles.header}><div className={styles.title}>{title}</div>{tools}</header> : null}
      {children}
    </section>
  );
}
