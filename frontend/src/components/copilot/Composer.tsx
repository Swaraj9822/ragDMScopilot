import { useEffect, useRef } from "react";
import { ArrowUp, Square } from "lucide-react";
import styles from "./Composer.module.css";

interface ComposerProps {
  value: string;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onStop?: () => void;
  submitting: boolean;
  includeSql: boolean;
  onToggleSql: (value: boolean) => void;
}

export function Composer({
  value,
  onChange,
  onSubmit,
  onStop,
  submitting,
  includeSql,
  onToggleSql,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Grow from 1 to 6 lines.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    const lineHeight = 22;
    const maxHeight = lineHeight * 6 + 20;
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
  }, [value]);

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      if (!submitting && value.trim()) onSubmit();
    }
  }

  const empty = value.trim().length === 0;

  return (
    <div className={styles.composer}>
      <label htmlFor="copilot-input" className="visually-hidden">
        Ask about your documents or business data
      </label>
      <textarea
        id="copilot-input"
        ref={textareaRef}
        className={`textarea ${styles.textarea}`}
        placeholder="Ask about your documents or business data…"
        value={value}
        rows={1}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={handleKeyDown}
        aria-describedby="composer-hint"
      />
      <div className={styles.row}>
        <label className={styles.sqlToggle}>
          <input
            type="checkbox"
            checked={includeSql}
            onChange={(e) => onToggleSql(e.target.checked)}
          />
          Show generated SQL
        </label>
        <span id="composer-hint" className={styles.hint}>
          Enter sends · Shift+Enter adds a line
        </span>
        <div className={styles.actions}>
          {submitting && onStop && (
            <button type="button" className="btn btn-sm" onClick={onStop}>
              <Square size={14} aria-hidden="true" />
              Stop waiting
            </button>
          )}
          <button
            type="button"
            className="btn btn-primary btn-icon"
            onClick={onSubmit}
            disabled={empty || submitting}
            aria-label="Send question"
          >
            <ArrowUp size={18} aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
  );
}
