import { describe, expect, it } from "vitest";
import { deriveColumns, renderCell } from "./rows";

describe("deriveColumns", () => {
  it("derives order from the first row, then appends new keys", () => {
    const rows = [
      { id: 1, name: "a" },
      { id: 2, name: "b", extra: true },
    ];
    expect(deriveColumns(rows)).toEqual(["id", "name", "extra"]);
  });

  it("returns empty for no rows", () => {
    expect(deriveColumns([])).toEqual([]);
  });
});

describe("renderCell", () => {
  it("keeps 0, false and empty string distinct from null", () => {
    expect(renderCell(0)).toBe("0");
    expect(renderCell(false)).toBe("false");
    expect(renderCell("")).toBe("");
    expect(renderCell(null)).toBe("—");
    expect(renderCell(undefined)).toBe("—");
  });

  it("serializes objects", () => {
    expect(renderCell({ a: 1 })).toBe('{"a":1}');
  });
});
