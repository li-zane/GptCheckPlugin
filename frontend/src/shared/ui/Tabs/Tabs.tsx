import type { ReactNode } from "react";

import { cx } from "../../lib/cx";
import styles from "./Tabs.module.css";

export type TabItem<T extends string> = { id: T; label: ReactNode; badge?: ReactNode };
export function Tabs<T extends string>({ active, items, onChange }: { active: T; items: readonly TabItem<T>[]; onChange: (id: T) => void }) {
  return <div className={styles.tabs} role="tablist">{items.map((item) => <button aria-selected={item.id === active} className={cx(styles.tab, item.id === active && styles.active)} key={item.id} onClick={() => onChange(item.id)} role="tab" type="button"><span>{item.label}</span>{item.badge}</button>)}</div>;
}
