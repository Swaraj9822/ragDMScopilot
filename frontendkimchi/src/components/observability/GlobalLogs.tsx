import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { RefreshCw, SearchX } from "lucide-react";
import { searchLogs } from "../../api/observability";
import { ApiError } from "../../api/client";
import { TRACE_ID_RE } from "../../lib/constants";
import { toIso } from "./traceFilterUtils";
import { EmptyState } from "../common/EmptyState";
import { ErrorState } from "../common/ErrorState";
import { Skeleton } from "../common/Skeleton";
import { LogList } from "./LogList";
import styles from "./GlobalLogs.module.css";

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];

interface GlobalLogsProps {
  onTraceClick: (traceId: string) => void;
}

interface LogFilterState {
  start: string;
  end: string;
  level: string;
  traceId: string;
  limit: number;
}

const DEFAULTS: LogFilterState = {
  start: "",
  end: "",
  level: "",
  traceId: "",
  limit: 100,
};

export function GlobalLogs({ onTraceClick }: GlobalLogsProps) {
  const [draft, setDraft] = useState<LogFilterState>(DEFAULTS);
  const [applied, setApplied] = useState<LogFilterState>(DEFAULTS);

  const traceIdInvalid = draft.traceId !== "" && !TRACE_ID_RE.test(draft.traceId);
  const limitInvalid = draft.limit < 1 || draft.limit > 1000;
  const rangeInvalid =
    draft.start !== "" &&
    draft.end !== "" &&
    new Date(draft.end).getTime() < new Date(draft.start).getTime();
  const canSearch = !traceIdInvalid && !limitInvalid && !rangeInvalid;

  const query = useQuery({
    queryKey: ["logs", "search", applied],
    queryFn: () =>
      searchLogs({
        start: toIso(applied.start),
        end: toIso(applied.end),
        level: applied.level || null,
        traceId: applied.traceId || null,
        limit: applied.limit,
      }),
    retry: false,
  });

  function set<K extends keyof LogFilterState>(key: K, value: LogFilterState[K]) {
    setDraft((prev) => ({ ...prev, [key]: value }));
  }

  return (
    <div className={styles.wrap}>
      <form
        className={styles.filters}
        onSubmit={(e) => {
          e.preventDefault();
          if (canSearch) setApplied(draft);
        }}
      >
        <div>
          <label className="field-label" htmlFor="log-level">
            Level
          </label>
          <select
            id="log-level"
            className="select"
            value={draft.level}
            onChange={(e) => set("level", e.target.value)}
          >
            <option value="">All</option>
            {LEVELS.map((l) => (
              <option key={l} value={l}>
                {l}
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="field-label" htmlFor="log-start">
            Start
          </label>
          <input
            id="log-start"
            type="datetime-local"
            className="input"
            value={draft.start}
            onChange={(e) => set("start", e.target.value)}
          />
        </div>
        <div>
          <label className="field-label" htmlFor="log-end">
            End
          </label>
          <input
            id="log-end"
            type="datetime-local"
            className="input"
            value={draft.end}
            onChange={(e) => set("end", e.target.value)}
            aria-invalid={rangeInvalid}
          />
          {rangeInvalid && (
            <span className={styles.err}>End must not be earlier than start.</span>
          )}
        </div>
        <div className={styles.traceField}>
          <label className="field-label" htmlFor="log-trace">
            Trace ID
          </label>
          <input
            id="log-trace"
            className="input"
            placeholder="32-char hex"
            value={draft.traceId}
            onChange={(e) => set("traceId", e.target.value.trim())}
            aria-invalid={traceIdInvalid}
          />
          {traceIdInvalid && (
            <span className={styles.err}>Must be 32 lowercase hex characters.</span>
          )}
        </div>
        <div>
          <label className="field-label" htmlFor="log-limit">
            Limit
          </label>
          <input
            id="log-limit"
            type="number"
            min={1}
            max={1000}
            className="input"
            value={draft.limit}
            onChange={(e) => set("limit", Number(e.target.value))}
            aria-invalid={limitInvalid}
          />
        </div>
        <div className={styles.actions}>
          <button type="submit" className="btn" disabled={!canSearch}>
            <RefreshCw size={14} aria-hidden="true" />
            Search
          </button>
        </div>
      </form>

      <div className={styles.results}>
        {query.isLoading ? (
          <div aria-busy="true" className={styles.loading}>
            <Skeleton height={14} />
            <Skeleton height={14} width="85%" />
            <Skeleton height={14} width="70%" />
          </div>
        ) : query.isError ? (
          <ErrorState
            title="Log search failed"
            body={query.error instanceof ApiError ? query.error.detail : undefined}
          />
        ) : !query.data || query.data.length === 0 ? (
          <EmptyState
            icon={SearchX}
            title="No logs match this search"
            body="Adjust the level, widen the time range, or clear the trace ID. An empty result is a valid answer."
          />
        ) : (
          <LogList logs={query.data} onTraceClick={onTraceClick} />
        )}
      </div>
    </div>
  );
}
