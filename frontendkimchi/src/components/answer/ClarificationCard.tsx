import { useState } from "react";
import { AlertCircle, HelpCircle, Loader2, Send } from "lucide-react";
import type {
  AbstentionResponse,
  ClarificationPrompt,
  UnifiedQueryResponse,
} from "../../api/types";
import { apiClient, ApiError, TIMEOUT_LONG_MS } from "../../api/client";
import styles from "./ClarificationCard.module.css";

/**
 * The backend responds to a clarification reply with either a full answer,
 * an abstention, or an error. This discriminated union captures the two
 * success shapes; errors are handled separately via ApiError.
 */
export type ClarificationResult =
  | { kind: "answer"; response: UnifiedQueryResponse }
  | { kind: "abstention"; response: AbstentionResponse };

export interface ClarificationCardProps {
  prompt: ClarificationPrompt;
  /** Called when the clarification reply returns a successful result. */
  onResult?: (result: ClarificationResult) => void;
}

/**
 * Renders a clarification question from the backend and provides an input for
 * the user to reply. On submission, posts to `/ask/clarify` and surfaces the
 * result or an inline error (expired/invalid clarification, network issues).
 *
 * Requirement: 2.3
 */
export function ClarificationCard({ prompt, onResult }: ClarificationCardProps) {
  const [reply, setReply] = useState("");
  const [status, setStatus] = useState<"idle" | "submitting">("idle");
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = reply.trim();
    if (!trimmed || status === "submitting") return;

    setError(null);
    setStatus("submitting");

    try {
      // The backend may return either a UnifiedQueryResponse (answer) or an
      // AbstentionResponse (abstention). We detect the shape by checking for
      // the `reason_code` field which only exists on an abstention payload.
      const data = await apiClient.postJson<UnifiedQueryResponse | AbstentionResponse>(
        "/ask/clarify",
        { clarification_id: prompt.clarification_id, reply: trimmed },
        { timeoutMs: TIMEOUT_LONG_MS },
      );

      if ("reason_code" in data && data.reason_code) {
        onResult?.({ kind: "abstention", response: data as AbstentionResponse });
      } else {
        onResult?.({ kind: "answer", response: data as UnifiedQueryResponse });
      }
    } catch (err) {
      if (err instanceof ApiError) {
        // Map known backend error codes to user-friendly messages.
        if (
          err.status === 400 &&
          err.detail.includes("clarification_invalid_or_expired")
        ) {
          setError(
            "This clarification has expired or is no longer valid. Please ask your question again.",
          );
        } else if (
          err.status === 400 &&
          err.detail.includes("clarification_reply_required")
        ) {
          setError("A reply is required. Please enter your response.");
        } else {
          setError(err.detail || "Something went wrong. Please try again.");
        }
      } else {
        setError("Unable to reach the server. Check your connection and try again.");
      }
    } finally {
      setStatus("idle");
    }
  }

  const isSubmitting = status === "submitting";

  return (
    <article className={styles.card} aria-label="Clarification needed">
      <div className={styles.header}>
        <HelpCircle size={20} className={styles.headerIcon} aria-hidden="true" />
        <p className={styles.question}>{prompt.clarification_question}</p>
      </div>

      <form className={styles.form} onSubmit={handleSubmit}>
        <div className={styles.inputWrapper}>
          <label htmlFor={`clarify-${prompt.clarification_id}`} className={styles.label}>
            Your reply
          </label>
          <textarea
            id={`clarify-${prompt.clarification_id}`}
            className={styles.input}
            value={reply}
            onChange={(e) => setReply(e.target.value)}
            placeholder="Type your answer to the clarification question…"
            rows={2}
            disabled={isSubmitting}
            aria-describedby={error ? `clarify-error-${prompt.clarification_id}` : undefined}
          />
        </div>

        {error && (
          <div
            className={styles.error}
            role="alert"
            id={`clarify-error-${prompt.clarification_id}`}
          >
            <AlertCircle size={16} className={styles.errorIcon} aria-hidden="true" />
            <span>{error}</span>
          </div>
        )}

        <div className={styles.actions}>
          <button
            type="submit"
            className="btn btn-sm btn-primary"
            disabled={isSubmitting || !reply.trim()}
          >
            {isSubmitting ? (
              <>
                <Loader2 size={14} aria-hidden="true" className="spin" />
                Sending…
              </>
            ) : (
              <>
                <Send size={14} aria-hidden="true" />
                Reply
              </>
            )}
          </button>
        </div>
      </form>

      {prompt.document_scope && prompt.document_scope.length > 0 && (
        <div className={styles.meta}>
          <span>
            Scoped to {prompt.document_scope.length}{" "}
            {prompt.document_scope.length === 1 ? "document" : "documents"}
          </span>
        </div>
      )}
    </article>
  );
}
