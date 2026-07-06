/**
 * Pure query-history persistence for SQL Lab (Requirement 11.3).
 *
 * When a query is executed, the SqlLabPage persists the submitted SQL to
 * browser localStorage as the *most recent* entry, retaining at most the 50
 * most recent submitted statements and evicting the oldest entry once that
 * limit is exceeded.
 *
 * The persistence logic is kept free of React so it can be imported directly by
 * a Vitest test (task 15.4 property-tests it) and by the page component. Both
 * `pushHistory` and `readHistory` accept an injected `Storage` (defaulting to
 * the browser's `localStorage`) so tests can drive them against an in-memory
 * mock without touching global state.
 *
 * Payloads are versioned and read defensively: malformed or absent stored JSON
 * is treated as an empty history rather than throwing. Wiring the failure
 * notice for a `setItem` quota error into the page is task 15.7; here
 * `pushHistory` returns the updated (in-memory) list so the caller can render
 * it regardless of whether the write to storage succeeded.
 */

import { LOCALSTORAGE_KEYS } from "../lib/constants";

/** The maximum number of history entries retained in localStorage. */
export const QUERY_HISTORY_LIMIT = 50;

/** The stable localStorage key SQL Lab query history is persisted under. */
export const QUERY_HISTORY_KEY = LOCALSTORAGE_KEYS.sqlLabHistory;

interface HistoryPayload {
  v: 1;
  /** Submitted SQL statements, newest-first. */
  items: string[];
}

/**
 * Resolve the Storage to operate on. Defaults to the browser `localStorage`
 * when available; returns `null` when no storage is reachable (e.g. SSR or a
 * disabled storage) so callers degrade gracefully to an empty history.
 */
function resolveStorage(storage?: Storage): Storage | null {
  if (storage) return storage;
  try {
    return globalThis.localStorage ?? null;
  } catch {
    return null;
  }
}

/**
 * Read the persisted query history, newest-first. Absent or malformed stored
 * JSON (including a payload of the wrong shape or version) is treated as an
 * empty history. Never throws.
 */
export function readHistory(storage?: Storage): string[] {
  const store = resolveStorage(storage);
  if (!store) return [];
  try {
    const raw = store.getItem(QUERY_HISTORY_KEY);
    if (raw === null) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (
      typeof parsed === "object" &&
      parsed !== null &&
      (parsed as HistoryPayload).v === 1 &&
      Array.isArray((parsed as HistoryPayload).items)
    ) {
      return (parsed as HistoryPayload).items.filter(
        (item): item is string => typeof item === "string",
      );
    }
    return [];
  } catch {
    return [];
  }
}

/**
 * Persist `sql` as the most recent history entry and return the updated list.
 *
 * The new entry is placed at the front (newest-first); the list is then capped
 * at {@link QUERY_HISTORY_LIMIT} entries by discarding the oldest (trailing)
 * entries. The updated list is returned even if writing to storage fails, so
 * the caller can reflect it in the UI (the persist-failure notice is task
 * 15.7).
 */
export function pushHistory(sql: string, storage?: Storage): string[] {
  const previous = readHistory(storage);
  const next = [sql, ...previous].slice(0, QUERY_HISTORY_LIMIT);

  const store = resolveStorage(storage);
  if (store) {
    const payload: HistoryPayload = { v: 1, items: next };
    store.setItem(QUERY_HISTORY_KEY, JSON.stringify(payload));
  }

  return next;
}
