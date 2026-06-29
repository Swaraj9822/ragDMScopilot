import { useState } from "react";
import { Check, Copy } from "lucide-react";
import styles from "./common.module.css";

interface CopyButtonProps {
  value: string;
  label?: string;
  /** Show only the icon (e.g. inside dense tables). */
  iconOnly?: boolean;
  size?: "sm" | "md";
}

export function CopyButton({ value, label = "Copy", iconOnly, size = "sm" }: CopyButtonProps) {
  const [copied, setCopied] = useState(false);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  }

  const text = copied ? "Copied" : label;
  return (
    <button
      type="button"
      className={`btn ${size === "sm" ? "btn-sm" : ""} ${iconOnly ? "btn-icon" : ""} ${styles.copyButton}`}
      onClick={handleCopy}
      aria-label={`${label}${copied ? " (copied)" : ""}`}
    >
      {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
      {!iconOnly && <span>{text}</span>}
    </button>
  );
}
