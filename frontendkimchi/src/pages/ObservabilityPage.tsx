import { useEffect, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Clock, MousePointerClick, RefreshCw, SearchX } from "lucide-react";
import { getTrace, searchTraces } from "../api/observability";
import { ApiError } from "../api/client";
import type { Trace } from "../api/types";
import { presetWindow, isConsoleTraffic, type TimePreset } from "../lib/observability";
import { TRACE_ID_RE } from "../lib/constants";
import { PageHeader } from "../components/common/PageHeader";
import { EmptyState } from "../components/common/EmptyState";
import { ErrorState } from "../components/common/ErrorState";
import { Skeleton } from "../components/common/Skeleton";
import { ViewSwitch, type ObsView } from "../components/observability/ViewSwitch";
import { SummaryStrip } from "../components/observability/SummaryStrip";
import { TraceFilters } from "../components/observability/TraceFilters";
import { TraceList } from "../components/observability/TraceList";
import { TraceDetail } from "../components/observability/TraceDetail";
import { GlobalLogs } from "../components/observability/GlobalLogs";
import { IndividualQueries } from "../components/observability/IndividualQueries";
import {
  DEFAULT_TRACE_FILTERS,
  toIso,
  validateTraceFilters,
  type TraceFilterState,
} from "../components/observability/traceFilterUtils";
import { useObservabilityPrefs } from "../hooks/useObservabilityPrefs";
import styles from "./ObservabilityPage.module.css";

const SUMMARY_CAP = 500;

function filtersFromParams(params: URLSearchParams): TraceFilterState {
  return {
    preset: (params.get("preset") as TimePreset) || DEFAULT_TRACE_FILTERS.preset,
    customStart: params.get("start") ?? "",
    customEnd: params.get("end") ?? "",
    route: params.get("route") ?? "",
    status: (params.get("status") as TraceFilterState["status"]) || "all",
    minDurationMs: params.get("min") ?? "",
    limit: Number(params.get("limit")) || DEFAULT_TRACE_FILTERS.limit,
  };
}

export default function ObservabilityPage() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { prefs, update } = useObservabilityPrefs();

  const viewParam = searchParams.get("view");
  // Landing on Observability without an explicit ?view always opens on Traces.
  // The active tab is tracked in the URL while on the page, but it is never
  // restored across navigations — arriving fresh should never drop you onto the
  // Individual Query or Logs tab.
  const view: ObsView =
    viewParam === "logs"
      ? "logs"
      : viewParam === "queries"
        ? "queries"
        : "traces";
  const selectedTraceId = searchParams.get("trace");
  const [filters, setFilters] = useState<TraceFilterState>(() =>
    filtersFromParams(searchParams),
  );

  const validation = validateTraceFilters(filters);

  // A stable descriptor for the query key. For presets we deliberately key on
  // the preset name (not resolved timestamps) so the cache stays stable across
  // auto-refreshes; the actual time window is resolved fresh inside queryFn on
  // every fetch so "last hour" always ends at the current moment.
  const queryDescriptor = useMemo(
    () => ({
      preset: filters.preset,
      customStart: filters.preset === "custom" ? toIso(filters.customStart) : null,
      customEnd: filters.preset === "custom" ? toIso(filters.customEnd) : null,
      route: filters.route || null,
      status: filters.status === "all" ? null : filters.status,
      minDurationMs: filters.minDurationMs ? Number(filters.minDurationMs) : null,
      limit: filters.limit,
    }),
    [
      filters.preset,
      filters.customStart,
      filters.customEnd,
      filters.route,
      filters.status,
      filters.minDurationMs,
      filters.limit,
    ],
  );

  const refetchInterval = prefs.autoRefresh ? prefs.intervalSeconds * 1000 : false;

  const tracesQuery = useQuery({
    queryKey: ["traces", queryDescriptor],
    queryFn: () => {
      // Resolve the window at fetch time so each refresh advances "now".
      const win =
        queryDescriptor.preset === "custom"
          ? { start: queryDescriptor.customStart, end: queryDescriptor.customEnd }
          : presetWindow(queryDescriptor.preset);
      return searchTraces({
        start: win.start,
        end: win.end,
        route: queryDescriptor.route,
        status: queryDescriptor.status,
        minDurationMs: queryDescriptor.minDurationMs,
        limit: queryDescriptor.limit,
      });
    },
    enabled: view === "traces" && validation.valid,
    refetchInterval,
    refetchIntervalInBackground: false, // pause while tab is hidden
    placeholderData: (prev) => prev, // avoid layout jumps on refresh
    retry: false,
  });

  // Apply the "hide console traffic" presentation filter client-side.
  const visibleTraces = useMemo(() => {
    const all = tracesQuery.data ?? [];
    const filtered = prefs.hideConsoleTraffic
      ? all.filter((t) => !isConsoleTraffic(t.route))
      : all;
    return filtered;
  }, [tracesQuery.data, prefs.hideConsoleTraffic]);

  const summarySample = visibleTraces.slice(0, SUMMARY_CAP);
  const availableRoutes = useMemo(
    () => Array.from(new Set((tracesQuery.data ?? []).map((t) => t.route))),
    [tracesQuery.data],
  );

  // Deep-link trace fetch when the selected trace is not in the current window.
  const traceInWindow = selectedTraceId
    ? (tracesQuery.data ?? []).find((t) => t.trace_id === selectedTraceId)
    : undefined;

  const deepLinkValid = !!selectedTraceId && TRACE_ID_RE.test(selectedTraceId);
  const deepLinkQuery = useQuery({
    queryKey: ["trace", selectedTraceId],
    queryFn: () => getTrace(selectedTraceId!),
    enabled: deepLinkValid && !traceInWindow,
    retry: false,
  });

  const selectedTrace: Trace | undefined = traceInWindow ?? deepLinkQuery.data;

  // Keep filters synced into the URL query string.
  useEffect(() => {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        next.set("view", view);
        applyFilterParams(next, filters);
        return next;
      },
      { replace: true },
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters]);

  function updateParam(mutate: (p: URLSearchParams) => void, replace = false) {
    setSearchParams(
      (prev) => {
        const next = new URLSearchParams(prev);
        mutate(next);
        return next;
      },
      { replace },
    );
  }

  function selectTrace(traceId: string) {
    updateParam((p) => p.set("trace", traceId), true);
  }

  function setView(next: ObsView) {
    updateParam((p) => p.set("view", next));
  }

  function jumpToTrace(traceId: string) {
    updateParam((p) => {
      p.set("view", "traces");
      p.set("trace", traceId);
    });
  }

  const atLimit = (tracesQuery.data?.length ?? 0) >= SUMMARY_CAP;

  return (
    <div>
      <PageHeader
        title="AI Observability"
        subtitle="Trace routing, retrieval, generation, SQL, and ingestion from request to result."
        actions={
          <div className={styles.headerActions}>
            <label className={styles.autoRefresh}>
              <input
                type="checkbox"
                checked={prefs.autoRefresh}
                onChange={(e) => update({ autoRefresh: e.target.checked })}
              />
              Auto-refresh
            </label>
            <select
              className="select"
              aria-label="Refresh interval"
              value={prefs.intervalSeconds}
              disabled={!prefs.autoRefresh}
              onChange={(e) =>
                update({ intervalSeconds: Number(e.target.value) as 5 | 10 | 30 })
              }
            >
              <option value={5}>5s</option>
              <option value={10}>10s</option>
              <option value={30}>30s</option>
            </select>
            <button
              type="button"
              className="btn btn-icon"
              aria-label="Refresh now"
              onClick={() => tracesQuery.refetch()}
            >
              <RefreshCw size={16} aria-hidden="true" />
            </button>
          </div>
        }
      />

      <div className={styles.controls}>
        <ViewSwitch value={view} onChange={setView} />
      </div>

      {view === "traces" ? (
        <div className={styles.tracesView}>
          <SummaryStrip
            traces={summarySample}
            hideConsoleTraffic={prefs.hideConsoleTraffic}
            onToggleConsoleTraffic={(v) => update({ hideConsoleTraffic: v })}
            atLimit={atLimit}
          />

          <TraceFilters
            filters={filters}
            routes={availableRoutes}
            onChange={setFilters}
            onClear={() => setFilters(DEFAULT_TRACE_FILTERS)}
          />

          <div className={`${styles.masterDetail} fullbleed`}>
            <div className={styles.master}>
              {tracesQuery.isLoading ? (
                <div aria-busy="true" className={styles.loading}>
                  <Skeleton height={36} />
                  <Skeleton height={36} />
                  <Skeleton height={36} />
                </div>
              ) : tracesQuery.isError ? (
                <ErrorState
                  title="Observability store unavailable"
                  body={
                    tracesQuery.error instanceof ApiError
                      ? tracesQuery.error.detail
                      : "Could not load traces. Copilot and Documents still work."
                  }
                />
              ) : visibleTraces.length === 0 ? (
                <EmptyState
                  icon={SearchX}
                  title="No traces match this window"
                  body="Try a wider time range, clear the route or status filters, or turn off Hide console traffic."
                  action={
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => setFilters(DEFAULT_TRACE_FILTERS)}
                    >
                      Clear filters
                    </button>
                  }
                />
              ) : (
                <TraceList
                  traces={visibleTraces}
                  selectedId={selectedTraceId}
                  onSelect={selectTrace}
                />
              )}
            </div>

            <div className={styles.detail}>
              {selectedTrace ? (
                <TraceDetail trace={selectedTrace} />
              ) : deepLinkQuery.isLoading ? (
                <div aria-busy="true" className={styles.loading}>
                  <Skeleton height={24} width="60%" />
                  <Skeleton height={120} />
                </div>
              ) : deepLinkQuery.isError ? (
                <EmptyState
                  icon={Clock}
                  title="This trace was not found"
                  body="It may have aged out of retention, or the id in the link is no longer available."
                />
              ) : selectedTraceId && !deepLinkValid ? (
                <EmptyState
                  icon={SearchX}
                  title="Malformed trace ID"
                  body="A trace id is 32 lowercase hexadecimal characters."
                />
              ) : (
                <EmptyState
                  icon={MousePointerClick}
                  title="Select a trace to inspect it"
                  body="Choose a row — or focus one and press Enter — to see its span waterfall and correlated logs."
                />
              )}
            </div>
          </div>
        </div>
      ) : view === "queries" ? (
        <IndividualQueries refetchInterval={refetchInterval} onTraceClick={jumpToTrace} />
      ) : (
        <GlobalLogs onTraceClick={jumpToTrace} />
      )}
    </div>
  );
}

function applyFilterParams(params: URLSearchParams, filters: TraceFilterState) {
  setOrDelete(params, "preset", filters.preset, DEFAULT_TRACE_FILTERS.preset);
  setOrDelete(params, "route", filters.route, "");
  setOrDelete(params, "status", filters.status, "all");
  setOrDelete(params, "min", filters.minDurationMs, "");
  setOrDelete(params, "limit", String(filters.limit), String(DEFAULT_TRACE_FILTERS.limit));
  if (filters.preset === "custom") {
    setOrDelete(params, "start", filters.customStart, "");
    setOrDelete(params, "end", filters.customEnd, "");
  } else {
    params.delete("start");
    params.delete("end");
  }
}

function setOrDelete(
  params: URLSearchParams,
  key: string,
  value: string,
  defaultValue: string,
) {
  if (value && value !== defaultValue) params.set(key, value);
  else params.delete(key);
}
