import fc from "fast-check";
import { describe, expect, it } from "vitest";
import {
  pushHistory,
  readHistory,
  QUERY_HISTORY_KEY,
  QUERY_HISTORY_LIMIT,
} from "./queryHistory";

/**
 * Feature: sql-lab, Property 18: Query history is bounded, newest-first, and
 * evicts the oldest.
 *
 * For any sequence of pushed SQL statements, the persisted history is
 * newest-first, contains at most QUERY_HISTORY_LIMIT (50) entries, and evicting
 * occurs by dropping the oldest. Pushing statements sequentially against a fresh
 * in-memory storage yields, for both the returned list and `readHistory`, the
 * last-up-to-50 pushed statements in reverse insertion order.
 *
 * Validates: Requirements 11.3
 */

/**
 * Minimal in-memory `Storage` implementation backed by a `Map`. Implements the
 * full DOM `Storage` surface (`getItem`/`setItem`/`removeItem`/`clear`/`key`/
 * `length`) so it can be injected into `pushHistory`/`readHistory` in place of
 * the browser `localStorage` without touching global state.
 */
class InMemoryStorage implements Storage {
  private store = new Map<string, string>();

  get length(): number {
    return this.store.size;
  }

  clear(): void {
    this.store.clear();
  }

  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null;
  }

  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null;
  }

  removeItem(key: string): void {
    this.store.delete(key);
  }

  setItem(key: string, value: string): void {
    this.store.set(key, String(value));
  }
}

/** The expected history for a sequence of pushes: newest-first, capped at 50. */
function expectedHistory(pushed: string[]): string[] {
  // Reverse insertion order (newest-first), then keep the first LIMIT entries.
  return [...pushed].reverse().slice(0, QUERY_HISTORY_LIMIT);
}

/** Generator for an arbitrary SQL-ish string (allowing duplicates & empties). */
const sqlArb: fc.Arbitrary<string> = fc.string({ maxLength: 40 });

describe("queryHistory property 18", () => {
  it("is bounded, newest-first, and evicts the oldest for any push sequence", () => {
    fc.assert(
      fc.property(fc.array(sqlArb, { maxLength: 120 }), (pushed) => {
        const storage = new InMemoryStorage();

        let returned: string[] = [];
        for (const sql of pushed) {
          returned = pushHistory(sql, storage);
        }

        const persisted = readHistory(storage);
        const expected = expectedHistory(pushed);

        // Bounded: never more than the limit.
        expect(returned.length).toBeLessThanOrEqual(QUERY_HISTORY_LIMIT);
        expect(persisted.length).toBeLessThanOrEqual(QUERY_HISTORY_LIMIT);

        // The returned list and the persisted list agree.
        expect(returned).toEqual(persisted);

        // Newest-first, reverse insertion order, oldest evicted past the limit.
        expect(returned).toEqual(expected);

        // Newest-first invariant: the last pushed statement is at index 0.
        if (pushed.length > 0) {
          expect(returned[0]).toBe(pushed[pushed.length - 1]);
        }
      }),
      { numRuns: 300 },
    );
  });

  it("evicts exactly the oldest entries once more than the limit are pushed", () => {
    fc.assert(
      fc.property(
        // Force strictly more than the limit by generating LIMIT + [1..70] items.
        fc
          .array(sqlArb, { minLength: 1, maxLength: 70 })
          .chain((extra) =>
            fc
              .array(sqlArb, {
                minLength: QUERY_HISTORY_LIMIT,
                maxLength: QUERY_HISTORY_LIMIT,
              })
              .map((base) => [...base, ...extra]),
          ),
        (pushed) => {
          const storage = new InMemoryStorage();
          for (const sql of pushed) pushHistory(sql, storage);

          const persisted = readHistory(storage);

          // Exactly at the cap.
          expect(persisted.length).toBe(QUERY_HISTORY_LIMIT);

          // Contains precisely the last LIMIT statements, newest-first.
          expect(persisted).toEqual(
            pushed.slice(-QUERY_HISTORY_LIMIT).reverse(),
          );

          // The statements pushed before the retained window were evicted.
          const evicted = pushed.slice(0, pushed.length - QUERY_HISTORY_LIMIT);
          const retainedWindow = new Set(pushed.slice(-QUERY_HISTORY_LIMIT));
          for (const [i, sql] of evicted.entries()) {
            // Only assert eviction for a value not present in the retained window.
            if (!retainedWindow.has(sql)) {
              expect(persisted).not.toContain(sql);
            }
            void i;
          }
        },
      ),
      { numRuns: 200 },
    );
  });

  it("persists under the stable QUERY_HISTORY_KEY", () => {
    const storage = new InMemoryStorage();
    pushHistory("SELECT 1", storage);
    expect(storage.getItem(QUERY_HISTORY_KEY)).not.toBeNull();
  });
});
