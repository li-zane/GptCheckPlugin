import { Search, X } from "lucide-react";
import type { InputHTMLAttributes } from "react";

import styles from "./SearchInput.module.css";

export function SearchInput({ onChange, value, ...props }: Omit<InputHTMLAttributes<HTMLInputElement>, "type">) {
  return <label className={styles.field}><Search aria-hidden="true" size={16} /><input {...props} onChange={onChange} type="search" value={value} />{value ? <button aria-label="清空搜索" onClick={() => onChange?.({ target: { value: "" } } as never)} title="清空搜索" type="button"><X size={15} /></button> : null}</label>;
}
