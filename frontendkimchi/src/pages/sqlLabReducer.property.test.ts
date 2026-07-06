import fc from "fast-check";
import { describe, expect, it } from "vitest";
import {
  initialSqlLabViewState,
  sqlLabReducer,
  type ResultSet,
  type SqlLabEvent,
  type SqlLabViewState,
} from "./sqlLabReducer";

/**
 * Feature: sql-lab, Property 9: The SqlLabPage renders exactly one state at a time.
 *
 * The pure reducer produces exactly one of `idle | loading | empty | result |
 * error` per lifecycle event. For any starting state and any sequence of events,
 * the reducer always yields exactly one well-formed variant with a valid `kind`,
 * and a `success` event resolves to `empty` iff its rows are empty else `result`.
 *
 * Validates: Requirements 6.6
 */

const VALID_KINDS = ["idle", "loading", "empty", "result", "error"] as const;
type Kind = (typeof VALID_KINDS)[number];

/** Generator for an arbitrary ResultSet. */
const resultSetArb: fc.Arbitrary<ResultSet> = fc
  .record({
    columns: fc.array(fc.string(), { maxLength: 5 }),
    rows: fc.array(
      fc.dictionary(fc.string(), fc.oneof(fc.string(), fc.integer(), fc.boolean())),
      { maxLength: 10 },
    ),
    durationMs: fc.nat(),
    sql: fc.string(),
    truncated: fc.boolean(),
  })
  .map((r) => ({ ...r, rowCount: r.rows.length }));

/** Generator for an arbitrary starting view state. */
const viewStateArb: fc.Arbitrary<SqlLabViewState> = fc.oneof(
  fc.constant<SqlLabViewState>({ kind: "idle" }),
  fc.constant<SqlLabViewState>({ kind: "loading" }),
  resultSetArb.map<SqlLabViewState>((result) => ({ kind: "empty", result })),
  resultSetArb.map<SqlLabViewState>((result) => ({ kind: "result", result })),
  fc.string().map<SqlLabViewState>((message) => ({ kind: "error", message })),
);

/** Generator for an arbitrary lifecycle event. */
const eventArb: fc.Arbitrary<SqlLabEvent> = fc.oneof(
  fc.constant<SqlLabEvent>({ type: "run" }),
  resultSetArb.map<SqlLabEvent>((result) => ({ type: "success", result })),
  fc.string().map<SqlLabEvent>((message) => ({ type: "error", message })),
  fc.constant<SqlLabEvent>({ type: "reset" }),
);

/**
 * Assert that a produced state is exactly one well-formed variant: its `kind`
 * is one of the five valid kinds and its payload matches that kind exactly.
 */
function expectExactlyOneWellFormedVariant(state: SqlLabViewState): void {
  // `kind` is a single string drawn from the valid set — never two at once.
  expect(VALID_KINDS).toContain(state.kind as Kind);

  const keys = Object.keys(state).sort();
  switch (state.kind) {
    case "idle":
    case "loading":
      expect(keys).toEqual(["kind"]);
      break;
    case "empty":
    case "result":
      expect(keys).toEqual(["kind", "result"]);
      expect((state as { result: ResultSet }).result).toBeTypeOf("object");
      break;
    case "error":
      expect(keys).toEqual(["kind", "message"]);
      expect((state as { message: string }).message).toBeTypeOf("string");
      break;
    default: {
      const _exhaustive: never = state;
      throw new Error(`unexpected kind: ${JSON.stringify(_exhaustive)}`);
    }
  }
}

describe("sqlLabReducer property 9", () => {
  it("yields exactly one well-formed variant for any state/event pair", () => {
    fc.assert(
      fc.property(viewStateArb, eventArb, (state, event) => {
        const next = sqlLabReducer(state, event);
        expectExactlyOneWellFormedVariant(next);
      }),
      { numRuns: 200 },
    );
  });

  it("resolves success to empty iff rows are empty, else result", () => {
    fc.assert(
      fc.property(viewStateArb, resultSetArb, (state, result) => {
        const next = sqlLabReducer(state, { type: "success", result });
        if (result.rows.length === 0) {
          expect(next).toEqual({ kind: "empty", result });
        } else {
          expect(next).toEqual({ kind: "result", result });
        }
      }),
      { numRuns: 200 },
    );
  });

  it("stays well-formed across any sequence of events from the initial state", () => {
    fc.assert(
      fc.property(fc.array(eventArb, { maxLength: 30 }), (events) => {
        let state = initialSqlLabViewState;
        for (const event of events) {
          state = sqlLabReducer(state, event);
          expectExactlyOneWellFormedVariant(state);
        }
      }),
      { numRuns: 200 },
    );
  });

  it("maps each event type to its expected kind independent of prior state", () => {
    fc.assert(
      fc.property(viewStateArb, eventArb, (state, event) => {
        const next = sqlLabReducer(state, event);
        switch (event.type) {
          case "run":
            expect(next.kind).toBe("loading");
            break;
          case "success":
            expect(["empty", "result"]).toContain(next.kind);
            break;
          case "error":
            expect(next.kind).toBe("error");
            break;
          case "reset":
            expect(next.kind).toBe("idle");
            break;
        }
      }),
      { numRuns: 200 },
    );
  });
});
