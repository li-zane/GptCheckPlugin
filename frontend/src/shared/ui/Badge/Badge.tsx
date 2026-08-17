import type { HTMLAttributes } from "react";

import { cx } from "../../lib/cx";
import styles from "./Badge.module.css";

export type BadgeTone = "neutral" | "success" | "warning" | "danger" | "info";

export function Badge({ className, tone = "neutral", ...props }: HTMLAttributes<HTMLSpanElement> & { tone?: BadgeTone }) {
  return <span className={cx(styles.badge, styles[tone], className)} data-tone={tone} {...props} />;
}
