import { Check, Copy } from "lucide-react";
import { useState } from "react";

import { IconButton } from "../IconButton/IconButton";

export function CopyButton({ value, label = "复制" }: { value: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return <IconButton aria-label={label} onClick={async () => { await navigator.clipboard.writeText(value); setCopied(true); window.setTimeout(() => setCopied(false), 1200); }} title={copied ? "已复制" : label}>{copied ? <Check size={16} /> : <Copy size={16} />}</IconButton>;
}
