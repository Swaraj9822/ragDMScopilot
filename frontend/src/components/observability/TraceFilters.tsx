import { ChevronDown, Filter, X } from "lucide-react";
import { useState } from "react";
import { TIME_PRESETS } from "../../lib/observability";
import {
  DEFAULT_TRACE_FILTERS,
  LIMIT_OPTIONS,
  validateTraceFilters,
  type TraceFilterState,
} from "./traceFilterUtils";
import styles from "./TraceFilters.module.css";

interface TraceFiltersProps {
  filters: TraceFilterState;
  routes: string[];
  onChange: (filters: TraceFilterState) => void;
  onClear: () => void;
}

const COMMON_ROUTES = ["/ask", "/query", "/copilot/query", "/documents", "ingestion"];

export function TraceFilters({ filters, routes, onChange, onClear }: TraceFiltersProps) {
  const [expanded, setExpanded] = useState(false);
  const validation = validateTraceFilters(filters);

  function set<K extends keyof TraceFilterState>(key: K, value: TraceFilterState[K]) {
    onChange({ ...filters, [key]: value });
  }

  const routeOptions = Array.from(new Set([...COMMON_ROUTES, ...routes]));

  return (
    <div className={styles.bar}>
      <div className={styles.barHead}>
        <button
          type="button"
          className={styles.expandToggle}
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          <Filter size={14} aria-hidden="true" />
          Filters
          <ChevronDown
            size={14}
            aria-hidden="true"
            style={{ transform: expanded ? "rotate(180deg)" : "none" }}
          />
        </button>
        <div className={styles.quick}>
          <label className="field-label" htmlFor="preset-quick">
            Time
          </label>
          <select
            id="preset-quick"
            className="select"
            value={filters.preset}
            onChange={(e) => set("preset", e.target.value as TraceFilterState["preset"])}
          >
            {TIME_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {expanded && (
        <div className={styles.grid}>
          {filters.preset === "custom" && (
            <>
              <div className={styles.fieldFull}>
                <label className="field-label" htmlFor="custom-start">
                  Start
                </label>
                <input
                  id="custom-start"
                  type="datetime-local"
                  className="input"
                  value={filters.customStart}
                  onChange={(e) => set("customStart", e.target.value)}
                />
              </div>
              <div className={styles.fieldFull}>
                <label className="field-label" htmlFor="custom-end">
                  End
                </label>
                <input
                  id="custom-end"
                  type="datetime-local"
                  className="input"
                  value={filters.customEnd}
                  onChange={(e) => set("customEnd", e.target.value)}
                />
                {validation.errors.customEnd && (
                  <span className={styles.err}>{validation.errors.customEnd}</span>
                )}
              </div>
            </>
          )}

          <div>
            <label className="field-label" htmlFor="route-filter">
              Route
            </label>
            <input
              id="route-filter"
              className="input"
              list="route-options"
              placeholder="Any route"
              value={filters.route}
              onChange={(e) => set("route", e.target.value)}
            />
            <datalist id="route-options">
              {routeOptions.map((r) => (
                <option key={r} value={r} />
              ))}
            </datalist>
          </div>

          <div>
            <label className="field-label" htmlFor="status-filter">
              Status
            </label>
            <select
              id="status-filter"
              className="select"
              value={filters.status}
              onChange={(e) => set("status", e.target.value as TraceFilterState["status"])}
            >
              <option value="all">All</option>
              <option value="success">success</option>
              <option value="error">error</option>
            </select>
          </div>

          <div>
            <label className="field-label" htmlFor="min-duration">
              Min duration (ms)
            </label>
            <input
              id="min-duration"
              type="number"
              min={0}
              max={86_400_000}
              className="input"
              placeholder="0"
              value={filters.minDurationMs}
              onChange={(e) => set("minDurationMs", e.target.value)}
            />
            {validation.errors.minDurationMs && (
              <span className={styles.err}>{validation.errors.minDurationMs}</span>
            )}
          </div>

          <div>
            <label className="field-label" htmlFor="limit-filter">
              Limit
            </label>
            <select
              id="limit-filter"
              className="select"
              value={filters.limit}
              onChange={(e) => set("limit", Number(e.target.value))}
            >
              {LIMIT_OPTIONS.map((l) => (
                <option key={l} value={l}>
                  {l}
                </option>
              ))}
            </select>
          </div>

          <div className={styles.clearWrap}>
            <button
              type="button"
              className="btn btn-sm"
              onClick={onClear}
              disabled={JSON.stringify(filters) === JSON.stringify(DEFAULT_TRACE_FILTERS)}
            >
              <X size={14} aria-hidden="true" />
              Clear filters
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
