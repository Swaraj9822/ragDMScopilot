import { describe, expect, it } from "vitest";
import {
  initialSqlLabViewState,
  sqlLabReducer,
  type ResultSet,
  type SqlLabViewState,
} from "./sqlLabReducer";

function makeResult(overrides: Partial<ResultSet> = {}): ResultSet {
  return {
    columns: ["id"],
    rows: [{ id: 1 }],
    rowCount: 1,
    durationMs: 5,
    sql: "SELECT 1",
    truncated: false,
    ...overrides,
  };
}

const everyState: SqlLabViewState[] = [
  { kind: "idle" },
  { kind: "loading" },
  { kind: "empty", result: makeResult({ rows: [], rowCount: 0 }) },
  { kind: "result", result: makeResult() },
  { kind: "error", message: "boom" },
];

describe("sqlLabReducer", () => {
  it("starts idle", () => {
    expect(initialSqlLabViewState).toEqual({ kind: "idle" });
  });

  it("moves to loading on run from any state", () => {
    for (const state of everyState) {
      expect(sqlLabReducer(state, { type: "run" })).toEqual({ kind: "loading" });
    }
  });

  it("resolves success with rows to the result state", () => {
    const result = makeResult({ rows: [{ id: 1 }, { id: 2 }], rowCount: 2 });
    expect(sqlLabReducer({ kind: "loading" }, { type: "success", result })).toEqual({
      kind: "result",
      result,
    });
  });

  it("resolves success with zero rows to the empty state", () => {
    const result = makeResult({ rows: [], rowCount: 0 });
    expect(sqlLabReducer({ kind: "loading" }, { type: "success", result })).toEqual({
      kind: "empty",
      result,
    });
  });

  it("moves to error carrying the message", () => {
    expect(
      sqlLabReducer({ kind: "loading" }, { type: "error", message: "db failed" }),
    ).toEqual({ kind: "error", message: "db failed" });
  });

  it("returns to idle on reset", () => {
    expect(sqlLabReducer({ kind: "error", message: "x" }, { type: "reset" })).toEqual({
      kind: "idle",
    });
  });

  it("produces a single valid kind for every state/event pair", () => {
    const events = [
      { type: "run" as const },
      { type: "success" as const, result: makeResult() },
      { type: "success" as const, result: makeResult({ rows: [], rowCount: 0 }) },
      { type: "error" as const, message: "e" },
      { type: "reset" as const },
    ];
    const validKinds = new Set(["idle", "loading", "empty", "result", "error"]);
    for (const state of everyState) {
      for (const event of events) {
        const next = sqlLabReducer(state, event);
        expect(validKinds.has(next.kind)).toBe(true);
      }
    }
  });
});
