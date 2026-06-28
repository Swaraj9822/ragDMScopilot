import { forwardRef, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDownUp, FileText } from "lucide-react";
import { getLogsByTrace } from "../../api/observability";
import { ApiError } from "../../api/client";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { Skeleton } from "../common/Skeleton";
import { LogList } from "./LogList";
import styles from "./CorrelatedLogs.module.css";

interface CorrelatedLogsProps {
  traceId: string;
}

// Lazily fetch logs only once a trace is selected.
export const CorrelatedLogs = forwardRef<HTMLDivElement, CorrelatedLogsProps>(
  function CorrelatedLogs({ traceId }, ref) {
    const [newestFirst, setNewestFirst] = useState(true);

    const query = useQuery({
      queryKey: ["logs", "trace", traceId],
      queryFn: () => getLogsByTrace(traceId),
      retry: false,
    });

    const ordered = useMemo(() => {
      if (!query.data) return [];
      // Backend returns newest-first; reverse locally without refetching.
      return newestFirst ? query.data : [...query.data].slice().reverse();
    }, [query.data, newestFirst]);

    return (
      <section className={styles.section} ref={ref} tabIndex={-1} aria-label="Correlated logs">
        <div className={styles.head}>
          <h3 className={styles.title}>Correlated logs</h3>
          {query.data && query.data.length > 0 && (
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => setNewestFirst((v) => !v)}
            >
              <ArrowDownUp size={14} aria-hidden="true" />
              {newestFirst ? "Newest first" : "Oldest first"}
            </button>
          )}
        </div>

        {query.isLoading ? (
          <div aria-busy="true" className={styles.loading}>
            <Skeleton height={14} />
            <Skeleton height={14} width="80%" />
          </div>
        ) : query.isError ? (
          <ErrorState
            title="Logs unavailable"
            body={
              query.error instanceof ApiError
                ? query.error.detail
                : "Could not load correlated logs. Trace detail is still shown above."
            }
          />
        ) : ordered.length === 0 ? (
          <EmptyState
            icon={FileText}
            title="No persisted logs for this trace"
            body="This trace produced no log records, or they have aged out of retention."
          />
        ) : (
          <LogList logs={ordered} showTraceId={false} />
        )}
      </section>
    );
  },
);
