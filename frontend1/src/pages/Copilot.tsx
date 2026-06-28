import { useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { ArrowUp, Square, RotateCcw, Sparkles } from "lucide-react";
import { api, type UnifiedQueryResponse } from "../api";
import styles from "./Copilot.module.css";

interface Exchange { id: string; question: string; answer: UnifiedQueryResponse; ms: number; }

const STARTERS = [
  "Summarize the key findings from uploaded documents",
  "What are the top revenue trends in the database?",
  "Which documents discuss compliance requirements?",
  "Run a SQL query for monthly active users",
];

export default function Copilot() {
  const [history, setHistory] = useState<Exchange[]>([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  const mutation = useMutation({
    mutationFn: (question: string) => {
      const controller = new AbortController();
      abortRef.current = controller;
      const t0 = performance.now();
      return api.ask(question, { signal: controller.signal }).then((r) => ({
        answer: r, ms: Math.round(performance.now() - t0),
      }));
    },
    onSuccess: ({ answer, ms }, question) => {
      setHistory((h) => [...h, { id: crypto.randomUUID(), question, answer, ms }]);
      setPending(null);
    },
    onError: () => setPending(null),
  });

  function send(q: string) {
    const trimmed = q.trim();
    if (!trimmed || mutation.isPending) return;
    setPending(trimmed);
    setInput("");
    mutation.mutate(trimmed);
    requestAnimationFrame(() => scrollRef.current?.scrollTo({ top: 999999, behavior: "smooth" }));
  }

  const empty = history.length === 0 && !pending;

  return (
    <div className={styles.page}>
      <div className={styles.messages} ref={scrollRef}>
        {empty && (
          <div className={styles.empty}>
            <Sparkles size={32} className={styles.emptyIcon} />
            <h1 className={styles.emptyTitle}>Ask your knowledge base</h1>
            <p className={styles.emptyDesc}>
              Questions are routed automatically to documents, database, or both.
            </p>
            <div className={styles.starters}>
              {STARTERS.map((s) => (
                <button key={s} className={styles.starter} onClick={() => send(s)} type="button">
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {history.map((ex) => (
          <div key={ex.id} className={styles.exchange}>
            <div className={styles.userMsg}>{ex.question}</div>
            <div className={styles.assistantMsg}>
              <div className={styles.prose}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{ex.answer.answer}</ReactMarkdown>
              </div>
              <div className={styles.meta}>
                <span className={`pill ${styles.routePill}`}>{ex.answer.route}</span>
                <span className={`pill ${styles.evidencePill}`}>{ex.answer.evidence_status}</span>
                <span className={styles.timing}>{ex.ms}ms</span>
                {ex.answer.trace_id && (
                  <a href={`/observability?trace=${ex.answer.trace_id}`} className={styles.traceLink}>
                    trace →
                  </a>
                )}
              </div>
              {ex.answer.sql && (
                <pre className={styles.sqlBlock}><code>{ex.answer.sql}</code></pre>
              )}
            </div>
          </div>
        ))}

        {pending && (
          <div className={styles.exchange}>
            <div className={styles.userMsg}>{pending}</div>
            <div className={styles.thinking}>
              <span /><span /><span />
            </div>
          </div>
        )}

        {mutation.isError && (
          <div className={styles.errorRow}>
            <span>Failed to get answer.</span>
            <button className="btn btn-sm" onClick={() => mutation.reset()} type="button">
              <RotateCcw size={12} /> Retry
            </button>
          </div>
        )}
      </div>

      <div className={styles.inputArea}>
        <div className={styles.inputWrap}>
          <textarea
            className={styles.input}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(input); } }}
            placeholder="Ask a question…"
            rows={1}
            aria-label="Question"
          />
          {mutation.isPending ? (
            <button className={`${styles.sendBtn} ${styles.stopBtn}`} onClick={() => abortRef.current?.abort()} type="button" aria-label="Stop">
              <Square size={16} />
            </button>
          ) : (
            <button className={styles.sendBtn} onClick={() => send(input)} disabled={!input.trim()} type="button" aria-label="Send">
              <ArrowUp size={16} />
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
