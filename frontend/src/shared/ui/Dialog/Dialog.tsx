import { useCallback, useRef, type ReactNode } from "react";
import { X } from "lucide-react";

import { useDialogFocus } from "../../hooks/useDialogFocus";
import { IconButton } from "../IconButton/IconButton";
import styles from "./Dialog.module.css";

export function Dialog({ children, footer, onClose, open, title }: { children: ReactNode; footer?: ReactNode; onClose: () => void; open: boolean; title: ReactNode }) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const close = useCallback(() => onClose(), [onClose]);
  useDialogFocus(open, dialogRef, close);
  if (!open) return null;
  return (
    <div className={styles.backdrop} onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <div aria-modal="true" className={styles.dialog} ref={dialogRef} role="dialog" tabIndex={-1}>
        <header className={styles.header}><h2>{title}</h2><IconButton aria-label="关闭" onClick={onClose} title="关闭"><X size={17} /></IconButton></header>
        <div className={styles.body}>{children}</div>
        {footer ? <footer className={styles.footer}>{footer}</footer> : null}
      </div>
    </div>
  );
}
