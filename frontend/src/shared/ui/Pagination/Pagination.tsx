import { ChevronLeft, ChevronRight } from "lucide-react";

import { IconButton } from "../IconButton/IconButton";
import styles from "./Pagination.module.css";

export function Pagination({ page, pageCount, onChange }: { page: number; pageCount: number; onChange: (page: number) => void }) {
  return <nav aria-label="分页" className={styles.pagination}><IconButton aria-label="上一页" disabled={page <= 1} onClick={() => onChange(page - 1)} title="上一页"><ChevronLeft size={16} /></IconButton><span>第 {page} / {Math.max(1, pageCount)} 页</span><IconButton aria-label="下一页" disabled={page >= pageCount} onClick={() => onChange(page + 1)} title="下一页"><ChevronRight size={16} /></IconButton></nav>;
}
