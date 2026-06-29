import { RefreshCw } from "lucide-react";
import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { CopilotExchange } from "../../hooks/useCopilotHistory";
import { AnswerCard } from "./AnswerCard";
import { RouteBadge } from "../common/RouteBadge";
import { Skeleton } from "../common/Skeleton";
import styles from "./ConversationView.module.css";

interface ConversationViewProps {
  exchanges: CopilotExchange[];
  pendingQuestion: string | null;
  error: { message: string; retriable: boolean } | null;
  onRetry: () => void;
  streamText: string;
  streamStage: string | null;
  streamRoute: string | null;
}

const STAGE_LABELS: Record<string, string> = {
  classifying: "Choosing the best route…",
  retrieving: "Searching documents…",
  generating: "Writing the answer…",
  generating_sql: "Generating SQL…",
  running_sql: "Querying the database…",
  gathering: "Gathering documents and data…",
  synthesizing: "Combining the sources…",
  composing: "Organizing the sources…",
};

export function ConversationView({
  exchanges,
  pendingQuestion,
  error,
  onRetry,
  streamText,
  streamStage,
  streamRoute,
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
          ) : streamText ? (
            <StreamingAnswer text={streamText} route={streamRoute} />
          ) : (
            <div className={styles.loading} aria-busy="true">
              <p className={styles.routing}>
                {(streamStage && STAGE_LABELS[streamStage]) ?? "Routing and gathering evidence…"}
              </p>
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

function StreamingAnswer({ text, route }: { text: string; route: string | null }) {
  return (
    <article className={`${styles.streaming} enter`} aria-label="Streaming answer" aria-live="polite">
      {route && (
        <div className={styles.streamHead}>
          <RouteBadge route={route} />
          <span className={styles.streamHint}>streaming…</span>
        </div>
      )}
      <div className={styles.streamProse}>
        <Markdown remarkPlugins={[remarkGfm]}>{text}</Markdown>
        <span className={styles.cursor} aria-hidden="true" />
      </div>
    </article>
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
