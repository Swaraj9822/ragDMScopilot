import { CopyButton } from "./CopyButton";
import styles from "./common.module.css";

interface CodeBlockProps {
  code: string;
  language?: string;
  /** Header label shown above the code. */
  title?: string;
}

// SQL and logs are always rendered as plain text, never executed or injected
// as HTML.
export function CodeBlock({ code, language, title }: CodeBlockProps) {
  return (
    <div className={styles.codeBlock}>
      {(title || language) && (
        <div className={styles.codeBlockHeader}>
          <span>{title ?? language}</span>
          <CopyButton value={code} label="Copy" />
        </div>
      )}
      <pre className={styles.codePre}>
        <code>{code}</code>
      </pre>
    </div>
  );
}
