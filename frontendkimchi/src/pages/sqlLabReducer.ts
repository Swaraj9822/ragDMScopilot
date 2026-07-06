/**
 * Pure view-state machine for the SQL Lab page.
 *
 * The page renders exactly one of the loading, empty, result, or error states at
 * any given time (Requirement 6.6). Modelling the view as a discriminated union
 * keyed on `kind` makes that guarantee structural: a `SqlLabViewState` value can
 * only ever be one variant, so the page can switch on `state.kind` and render a
 * single branch.
 *
 * This module is intentionally free of React so it can be imported by both the
 * page component and a Vitest test.
 */

/**
 * A successful query result returned by `POST /sql/run`.
 *
 * Mirrors the backend `Result_Set` contract. Declared here so the reducer is
 * self-contained; the typed API module re-uses the same shape.
 */
export interface ResultSet {
  columns: string[];
  rows: Record<string, unknown>[];
  rowCount: number;
  durationMs: number;
  sql: string;
  truncated: boolean;
}

/**
 * The view state. Exactly one variant is active at a time.
 *
 * - `idle`: nothing has been run yet.
 * - `loading`: a query request is in flight.
 * - `empty`: the query succeeded and returned zero rows.
 * - `result`: the query succeeded and returned at least one row.
 * - `error`: the request failed (guard rejection, db error, timeout, or auth).
 */
export type SqlLabViewState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "empty"; result: ResultSet }
  | { kind: "result"; result: ResultSet }
  | { kind: "error"; message: string };

/**
 * Lifecycle events that drive the view state.
 *
 * - `run`: a query was submitted; the request has begun.
 * - `success`: the request resolved with a `ResultSet`.
 * - `error`: the request failed with a human-readable message.
 * - `reset`: return to the initial idle state.
 */
export type SqlLabEvent =
  | { type: "run" }
  | { type: "success"; result: ResultSet }
  | { type: "error"; message: string }
  | { type: "reset" };

/** The initial state before any query is run. */
export const initialSqlLabViewState: SqlLabViewState = { kind: "idle" };

/**
 * Pure reducer producing exactly one of `idle | loading | empty | result | error`
 * per lifecycle event.
 *
 * The reducer is total and deterministic: any event applied to any state yields
 * a single well-formed view state, so the page never shows two states at once
 * (Requirement 6.6). A `success` event resolves to `empty` when the result has
 * no rows and to `result` otherwise.
 */
export function sqlLabReducer(
  _state: SqlLabViewState,
  event: SqlLabEvent,
): SqlLabViewState {
  switch (event.type) {
    case "run":
      return { kind: "loading" };
    case "success":
      return event.result.rows.length === 0
        ? { kind: "empty", result: event.result }
        : { kind: "result", result: event.result };
    case "error":
      return { kind: "error", message: event.message };
    case "reset":
      return { kind: "idle" };
    default: {
      // Exhaustiveness guard: adding a new event without handling it is a
      // compile-time error.
      const _exhaustive: never = event;
      return _exhaustive;
    }
  }
}
