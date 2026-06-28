import { RefreshCw } from "lucide-react";
import type { CopilotExchange } from "../../hooks/useCopilotHistory";
import { AnswerCard } from "./AnswerCard";
import { Skeleton } from "../common/Skeleton";
import styles from "./ConversationView.module.css";

interface ConversationViewProps {
  exchanges: CopilotExchange[];
  pendingQuestion: string | null;
  error: { message: string; retriable: boolean } | null;
  onRetry: () => void;
}

export function ConversationView({
  exchanges,
  pendingQuestion,
  error,
  onRetry,
}: ConversationViewProps) {
  return (
    <div className={styles.thread}>
      {exchanges.map((ex) => (
        <div key={ex.id} className={styles.exchange}>
          <UserMessage text={ex.question} />
          <AnswerCard response={ex.response} elapsedMs={ex.elapsedMs} />
        </div>
      ))}

      {pendingQuestion !== null && (
        <div className={styles.exchange}>
          <UserMessage text={pendingQuestion} />
          {error ? (
            <div className={styles.error} role="alert">
              <p>{error.message}</p>
              {error.retriable && (
                <button type="button" className="btn btn-sm" onClick={onRetry}>
                  <RefreshCw size={14} aria-hidden="true" />
                  Retry
                </button>
              )}
            </div>
          ) : (
            <div className={styles.loading} aria-busy="true">
              <p className={styles.routing}>Routing and gathering evidence…</p>
              <div className={styles.progressLine} />
              <Skeleton height={14} width="90%" />
              <Skeleton height={14} width="80%" />
              <Skeleton height={14} width="60%" />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function UserMessage({ text }: { text: string }) {
  return (
    <div className={styles.userMessage}>
      <span className={styles.userLabel}>You</span>
      <p className={styles.userText}>{text}</p>
    </div>
  );
}
