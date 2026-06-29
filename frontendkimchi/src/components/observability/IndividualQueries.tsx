import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileUp, MessageSquareText, SearchX } from "lucide-react";
import { searchTraces } from "../../api/observability";
import { ApiError } from "../../api/client";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { Skeleton } from "../common/Skeleton";
import { formatAbsolute, formatDuration, shortenId } from "../../lib/format";
import {
  buildIndividualEntries,
  confidenceBand,
  type IndividualEntry,
} from "../../lib/individualQueries";
import styles from "./IndividualQueries.module.css";

interface IndividualQueriesProps {
  /** Auto-refresh cadence inherited from the page, or false when paused. */
  refetchInterval: number | false;
  /** Jump to the full trace waterfall for a row. */
  onTraceClick: (traceId: string) => void;
}

export function IndividualQueries({ refetchInterval, onTraceClick }: IndividualQueriesProps) {
  const query = useQuery({
    queryKey: ["individual-queries"],
    queryFn: () => searchTraces({ limit: 200 }),
    refetchInterval,
    refetchIntervalInBackground: false,
    placeholderData: (prev) => prev,
    retry: false,
  });

  const entries = useMemo(() => buildIndividualEntries(query.data ?? []), [query.data]);

  if (query.isLoading) {
    return (
      <div aria-busy="true" className={styles.loading}>
        <Skeleton height={40} />
        <Skeleton height={40} />
        <Skeleton height={40} />
      </div>
    );
  }

  if (query.isError) {
    return (
      <ErrorState
        title="Observability store unavailable"
        body={
          query.error instanceof ApiError
            ? query.error.detail
            : "Could not load query and upload activity."
        }
      />
    );
  }

  if (entries.length === 0) {
    return (
      <EmptyState
        icon={SearchX}
        title="No queries or uploads yet"
        body="Ask a question on the Copilot tab or upload a document, then come back to see it here."
      />
    );
  }

  return (
    <div className={`${styles.wrap} fullbleed`}>
      <table className={styles.table}>
        <caption className="visually-hidden">
          Individual queries and document uploads with latency, confidence, and token usage.
        </caption>
        <thead>
          <tr>
            <th scope="col">Type</th>
            <th scope="col">Question / File</th>
            <th scope="col" className={styles.num}>
              Latency
            </th>
            <th scope="col" className={styles.num}>
              Confidence
            </th>
            <th scope="col" className={styles.num}>
              Tokens
            </th>
            <th scope="col">When</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <EntryRow key={entry.traceId} entry={entry} onClick={() => onTraceClick(entry.traceId)} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function EntryRow({ entry, onClick }: { entry: IndividualEntry; onClick: () => void }) {
  const isQuery = entry.kind === "query";
  const band = confidenceBand(entry.confidenceScore);

  return (
    <tr
      className={styles.row}
      onClick={onClick}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onClick();
        }
      }}
      tabIndex={0}
      role="button"
      aria-label={`Open trace ${shortenId(entry.traceId)}`}
    >
      <td>
        <span className={`${styles.kind} ${isQuery ? styles.kindQuery : styles.kindUpload}`}>
          {isQuery ? <MessageSquareText size={13} aria-hidden="true" /> : <FileUp size={13} aria-hidden="true" />}
          {isQuery ? "Query" : "Upload"}
        </span>
      </td>
      <td className={styles.detail}>
        {isQuery ? (
          <span className={styles.question} title={entry.question ?? undefined}>
            {entry.question ?? <span className={styles.muted}>question unavailable</span>}
          </span>
        ) : (
          <span className={styles.filename} title={entry.filename ?? undefined}>
            {entry.filename ?? <span className={styles.muted}>file unavailable</span>}
          </span>
        )}
        {entry.status === "error" && <span className={styles.errorTag}>error</span>}
      </td>
      <td className={styles.num}>{formatDuration(entry.durationMs)}</td>
      <td className={styles.num}>
        {isQuery && entry.confidenceScore !== null && band ? (
          <span className={`${styles.confidence} ${styles[`band_${band}`]}`}>
            {entry.confidenceScore.toFixed(2)}
          </span>
        ) : (
          <span className={styles.muted}>—</span>
        )}
      </td>
      <td className={styles.num}>
        {isQuery && entry.totalTokens !== null ? (
          entry.totalTokens.toLocaleString()
        ) : (
          <span className={styles.muted}>—</span>
        )}
      </td>
      <td>
        <time className={styles.when} dateTime={entry.startTs} title={formatAbsolute(entry.startTs)}>
          {formatAbsolute(entry.startTs)}
        </time>
      </td>
    </tr>
  );
}
