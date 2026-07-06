import fc from "fast-check";
import { describe, expect, it } from "vitest";
import { boundedInsight, MAX_INSIGHT_LENGTH } from "./boundedInsight";

/**
 * Feature: sql-lab, Property 16: The rendered insight line is bounded.
 *
 * For any input insight string (or null/undefined), boundedInsight returns at
 * most one line of at most MAX_INSIGHT_LENGTH (200) characters, or null when the
 * insight is absent or blank/whitespace-only once collapsed. The result never
 * contains a newline character (it is always a single line).
 *
 * Validates: Requirements 10.5
 */

/**
 * A generator that biases toward the interesting corners of the input space:
 * strings with embedded newlines and tabs, very long strings (> 200 chars),
 * and whitespace-only strings, alongside arbitrary unicode strings.
 */
const insightArb: fc.Arbitrary<string> = fc.oneof(
  // Arbitrary unicode strings (any content, any length).
  fc.string(),
  fc.string({ unit: "grapheme" }),
  // Whitespace-only strings (spaces, tabs, newlines, carriage returns).
  fc
    .array(fc.constantFrom(" ", "\t", "\n", "\r", "\f", "\v"), { maxLength: 20 })
    .map((chars) => chars.join("")),
  // Very long strings guaranteed to exceed the 200-char bound.
  fc
    .string({ minLength: MAX_INSIGHT_LENGTH + 1, maxLength: 2000 })
    .filter((s) => s.length > MAX_INSIGHT_LENGTH),
  // Strings with interleaved newlines/tabs and real content.
  fc
    .array(
      fc.oneof(
        fc.string(),
        fc.constantFrom("\n", "\t", "\r\n", "  ", "\r", "\f"),
      ),
      { maxLength: 30 },
    )
    .map((parts) => parts.join("")),
);

describe("boundedInsight property 16", () => {
  it("returns null for null or undefined input", () => {
    expect(boundedInsight(null)).toBeNull();
    expect(boundedInsight(undefined)).toBeNull();
    expect(boundedInsight()).toBeNull();
  });

  it("returns null or a bounded single line for any string input", () => {
    fc.assert(
      fc.property(insightArb, (insight) => {
        const result = boundedInsight(insight);

        if (result === null) {
          // Null is only produced when the collapsed insight is blank.
          expect(insight.replace(/\s+/g, " ").trim().length).toBe(0);
          return;
        }

        // Non-null result is a string...
        expect(typeof result).toBe("string");
        // ...of at most MAX_INSIGHT_LENGTH characters...
        expect(result.length).toBeLessThanOrEqual(MAX_INSIGHT_LENGTH);
        // ...that is a single line (no newline characters of any kind)...
        expect(result).not.toMatch(/[\r\n]/);
        // A non-null result is non-empty (blank collapses to null).
        expect(result.length).toBeGreaterThan(0);
      }),
      { numRuns: 1000 },
    );
  });

  it("maps blank/whitespace-only input to null", () => {
    fc.assert(
      fc.property(
        fc
          .array(fc.constantFrom(" ", "\t", "\n", "\r", "\f", "\v"), {
            maxLength: 50,
          })
          .map((chars) => chars.join("")),
        (whitespace) => {
          expect(boundedInsight(whitespace)).toBeNull();
        },
      ),
      { numRuns: 500 },
    );
  });

  it("truncates long single-line content to exactly MAX_INSIGHT_LENGTH", () => {
    fc.assert(
      fc.property(
        // Non-whitespace content longer than the bound, no whitespace so
        // collapsing does not shorten it.
        fc
          .string({ minLength: MAX_INSIGHT_LENGTH + 1, maxLength: 1000 })
          .map((s) => s.replace(/\s/g, "x"))
          .filter((s) => s.length > MAX_INSIGHT_LENGTH),
        (longContent) => {
          const result = boundedInsight(longContent);
          expect(result).not.toBeNull();
          expect(result!.length).toBe(MAX_INSIGHT_LENGTH);
        },
      ),
      { numRuns: 500 },
    );
  });
});
