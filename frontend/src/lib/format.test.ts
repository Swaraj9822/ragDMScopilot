import { describe, expect, it } from "vitest";
import { formatBytes, formatDuration, shortenId } from "./format";

describe("formatDuration", () => {
  it("formats sub-second durations in ms", () => {
    expect(formatDuration(842)).toBe("842 ms");
  });
  it("formats seconds with two decimals", () => {
    expect(formatDuration(1420)).toBe("1.42 s");
  });
  it("formats minutes and padded seconds", () => {
    expect(formatDuration(128_000)).toBe("2m 08s");
  });
  it("guards against negative input", () => {
    expect(formatDuration(-5)).toBe("—");
  });
});

describe("formatBytes", () => {
  it("formats bytes and larger units", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KiB");
  });
});

describe("shortenId", () => {
  it("keeps short ids intact", () => {
    expect(shortenId("abc")).toBe("abc");
  });
  it("elides long ids", () => {
    expect(shortenId("a".repeat(32))).toContain("…");
  });
});
