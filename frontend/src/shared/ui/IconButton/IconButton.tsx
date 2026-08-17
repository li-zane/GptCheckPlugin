import { forwardRef, type ButtonHTMLAttributes } from "react";

import { cx } from "../../lib/cx";
import styles from "./IconButton.module.css";

export const IconButton = forwardRef<HTMLButtonElement, ButtonHTMLAttributes<HTMLButtonElement>>(
  function IconButton({ className, type = "button", ...props }, ref) {
    return <button className={cx(styles.button, className)} ref={ref} type={type} {...props} />;
  },
);
