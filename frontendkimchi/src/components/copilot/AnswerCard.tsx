import { useState } from "react";
import { Link } from "react-router-dom";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  ThumbsDown,
  ThumbsUp,
} from "lucide-react";
import type { QueryFeedbackRequest, UnifiedQueryResponse } from "../../api/types";
import { submitFeedback } from "../../api/copilot";
import { ApiError } from "../../api/client";
import { useToast } from "../../hooks/useToast";
import { RouteBadge } from "../common/RouteBadge";
import { StatusBadge } from "../common/StatusBadge";
import { CopyButton } from "../common/CopyButton";
import { CodeBlock } from "../common/CodeBlock";
import { RowsTable } from "./RowsTable";
import { formatDuration, shortenId } from "../../lib/format";
import styles from "./AnswerCard.module.css";

interface AnswerCardProps {
  response: UnifiedQueryResponse;
  elapsedMs: number;
}

export function AnswerCard({ response, elapsedMs }: AnswerCardProps) {
  const {
    answer,
    route,
    evidence_status,
    trace_id,
    citations,
    insufficient_evidence_reason,
    sql,
    rows,
    data_sources,
    routing_reasoning,
  } = response;

  return (
    <article className={`${styles.card} enter`} aria-label="Copilot answer">
      <div className={styles.prose}>
        {answer ? (
          // react-markdown does not render raw HTML by default (no rehype-raw),
          // so this is safe against HTML injection.
          <Markdown remarkPlugins={[remarkGfm]}>{answer}</Markdown>
        ) : (
          <p className="muted">The service returned no answer.</p>
        )}
      </div>

      {insufficient_evidence_reason && (
        <div className={styles.callout} role="note">
          <AlertTriangle size={16} aria-hidden="true" />
          <span>{insufficient_evidence_reason}</span>
        </div>
      )}

      <div className={styles.metaRow}>
        <RouteBadge route={route} />
        <StatusBadge status={evidence_status} label={evidence_status.replaceAll("_", " ")} />
        <span className={styles.metaItem}>{formatDuration(elapsedMs)}</span>
        {trace_id && (
          <span className={styles.traceId}>
            <span className="mono">{shortenId(trace_id, 10, 6)}</span>
            <CopyButton value={trace_id} label="Copy trace ID" iconOnly />
          </span>
        )}
      </div>

      {(citations.length > 0 ||
        data_sources.length > 0 ||
        sql ||
        rows.length > 0 ||
        routing_reasoning) && (
        <div className={styles.evidence}>
          {citations.length > 0 && (
            <Section title={`Citations (${citations.length})`} defaultOpen>
              <div className={styles.citations}>
                {citations.map((c, i) => (
                  <div key={`${c.chunk_id}-${i}`} className={styles.citation}>
                    <div className={styles.citationHead}>
                      <span className={styles.citationNum}>{i + 1}</span>
                      <span className={styles.citationTitle}>{c.title ?? "Untitled source"}</span>
                    </div>
                    <dl className={styles.citationMeta}>
                      {(c.page_start != null || c.page_end != null) && (
                        <span>
                          Pages {c.page_start ?? "?"}
                          {c.page_end != null && c.page_end !== c.page_start ? `–${c.page_end}` : ""}
                        </span>
                      )}
                    </dl>
                    <div className={styles.citationIds}>
                      <span className="mono meta">doc {shortenId(c.document_id)}</span>
                      <CopyButton value={c.document_id} label="Copy document ID" iconOnly />
                      <span className="mono meta">chunk {shortenId(c.chunk_id)}</span>
                      <CopyButton value={c.chunk_id} label="Copy chunk ID" iconOnly />
                    </div>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {data_sources.length > 0 && (
            <Section title={`Data sources (${data_sources.length})`}>
              <div className={styles.sources}>
                {data_sources.map((src) => (
                  <div key={src.table} className={styles.source}>
                    <span className="mono">{src.table}</span>
                    <span className="meta">{src.columns.join(", ")}</span>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {sql && (
            <Section title="Generated SQL">
              <CodeBlock code={sql} title="SQL" />
            </Section>
          )}

          {rows.length > 0 && (
            <Section title={`Result rows (${rows.length})`}>
              <RowsTable rows={rows} />
            </Section>
          )}

          {routing_reasoning && (
            <Section title="Why this route?">
              <p className={styles.reasoning}>{routing_reasoning}</p>
            </Section>
          )}
        </div>
      )}

      <div className={styles.actions}>
        <CopyButton value={answer} label="Copy answer" />
        {trace_id && (
          <Link to={`/observability?trace=${trace_id}`} className="btn btn-sm">
            <ExternalLink size={14} aria-hidden="true" />
            Inspect trace
          </Link>
        )}
      </div>

      {trace_id && <AnswerFeedback traceId={trace_id} />}
    </article>
  );
}

function Section({
  title,
  defaultOpen = false,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <section className={styles.section}>
      <button
        type="button"
        className={styles.sectionToggle}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {title}
      </button>
      {open && <div className={styles.sectionBody}>{children}</div>}
    </section>
  );
}

/**
 * Operator feedback control. A thumbs-up submits immediately (rating 5); a
 * thumbs-down opens an optional comment / expected-answer form before sending
 * (rating 1) — that is when the extra detail is worth capturing. Feedback is
 * keyed by the answer's trace id and posted to /queries/{trace_id}/feedback.
 */
function AnswerFeedback({ traceId }: { traceId: string }) {
  const { pushToast } = useToast();
  const [choice, setChoice] = useState<"up" | "down" | null>(null);
  const [status, setStatus] = useState<"idle" | "submitting" | "done">("idle");
  const [comment, setComment] = useState("");
  const [expected, setExpected] = useState("");

  async function send(rating: number, detail?: { comment: string; expected: string }) {
    setStatus("submitting");
    const payload: QueryFeedbackRequest = {
      rating,
      comment: detail?.comment.trim() ? detail.comment.trim() : null,
      expected_answer: detail?.expected.trim() ? detail.expected.trim() : null,
    };
    try {
      await submitFeedback(traceId, payload);
      setStatus("done");
      pushToast("Thanks — your feedback was recorded.", "success");
    } catch (err) {
      setStatus("idle");
      if (err instanceof ApiError && err.status === 404) {
        pushToast("This answer isn't saved yet. Try again in a moment.", "warning");
      } else {
        pushToast("Couldn't record feedback. Please try again.", "error");
      }
    }
  }

  if (status === "done") {
    return (
      <div className={styles.feedbackDone} role="status">
        <Check size={14} aria-hidden="true" />
        Thanks for the feedback.
      </div>
    );
  }

  return (
    <div className={styles.feedback}>
      <div className={styles.feedbackPrompt}>
        <span className={styles.feedbackLabel}>Was this answer helpful?</span>
        <div className={styles.feedbackButtons}>
          <button
            type="button"
            className={`${styles.thumb} ${choice === "up" ? styles.thumbActive : ""}`}
            onClick={() => {
              setChoice("up");
              void send(5);
            }}
            disabled={status === "submitting"}
            aria-pressed={choice === "up"}
          >
            <ThumbsUp size={15} aria-hidden="true" />
            Helpful
          </button>
          <button
            type="button"
            className={`${styles.thumb} ${choice === "down" ? styles.thumbActive : ""}`}
            onClick={() => setChoice("down")}
            disabled={status === "submitting"}
            aria-pressed={choice === "down"}
          >
            <ThumbsDown size={15} aria-hidden="true" />
            Not helpful
          </button>
        </div>
      </div>

      {choice === "down" && (
        <div className={styles.feedbackForm}>
          <label className={styles.feedbackField}>
            <span className={styles.feedbackFieldLabel}>What went wrong? (optional)</span>
            <textarea
              className={styles.feedbackTextarea}
              value={comment}
              onChange={(e) => setComment(e.target.value)}
              rows={2}
              maxLength={2000}
              placeholder="Missing context, wrong citation, off-topic…"
            />
          </label>
          <label className={styles.feedbackField}>
            <span className={styles.feedbackFieldLabel}>Expected answer (optional)</span>
            <textarea
              className={styles.feedbackTextarea}
              value={expected}
              onChange={(e) => setExpected(e.target.value)}
              rows={2}
              maxLength={5000}
              placeholder="What should the answer have said?"
            />
          </label>
          <div className={styles.feedbackFormActions}>
            <button
              type="button"
              className="btn btn-sm btn-primary"
              onClick={() => void send(1, { comment, expected })}
              disabled={status === "submitting"}
            >
              {status === "submitting" ? "Sending…" : "Send feedback"}
            </button>
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setChoice(null)}
              disabled={status === "submitting"}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
