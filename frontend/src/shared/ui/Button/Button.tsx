import { forwardRef, type ButtonHTMLAttributes } from "react";

import { cx } from "../../lib/cx";
import styles from "./Button.module.css";

export type ButtonTone = "primary" | "secondary" | "danger" | "ghost";

export const Button = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement> & { tone?: ButtonTone }>(
  function Button({ className, tone = "secondary", type = "button", ...props }, ref) {
    return <button className={cx(styles.button, styles[tone], className)} ref={ref} type={type} {...props} />;
  },
);
