import styles from "./ExamplePrompts.module.css";

const EXAMPLES = [
  "Summarize the key policy changes in the uploaded documents",
  "What was total sales this month?",
  "Which customer generated the most revenue?",
  "Compare the documented refund policy with recent refund data",
];

export function ExamplePrompts({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className={styles.wrap}>
      <h2 className={styles.heading}>Ask across documents and business data</h2>
      <p className={styles.copy}>
        The copilot will choose document search, database analysis, or both.
      </p>
      <div className={styles.grid}>
        {EXAMPLES.map((text) => (
          <button
            key={text}
            type="button"
            className={styles.example}
            onClick={() => onPick(text)}
          >
            {text}
          </button>
        ))}
      </div>
    </div>
  );
}
