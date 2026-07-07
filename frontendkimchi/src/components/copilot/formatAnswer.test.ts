import { describe, expect, it } from "vitest";
import { formatAnswer } from "./formatAnswer";

describe("formatAnswer", () => {
  it("returns free-form (non-template) answers unchanged", () => {
    const prose = "The policy allows refunds within 30 days of purchase.";
    expect(formatAnswer(prose, [])).toBe(prose);
  });

  it("returns an empty answer unchanged", () => {
    expect(formatAnswer("", [])).toBe("");
  });

  it("drops the 'Summary:' label from the lead text", () => {
    const out = formatAnswer("Summary: Public Cheese 1L leads sales.", []);
    expect(out).toBe("Public Cheese 1L leads sales.");
    expect(out).not.toMatch(/summary/i);
  });

  it("removes the raw 'Results:' JSON block from the output", () => {
    const answer =
      'Summary: Cheese leads. Results: [{ "item_name": "Cheese", "qty": 640 }] Conclusion: Cheese wins.';
    const out = formatAnswer(answer, [{ item_name: "Cheese", qty: 640 }]);
    expect(out).not.toMatch(/results\s*:/i);
    expect(out).not.toContain("item_name");
    expect(out).not.toContain("[{");
  });

  it("presents structured rows as bullet points between the lead and conclusion", () => {
    const answer =
      'Summary: Public Cheese 1L is the top seller. Results: [ ... ] Conclusion: Public Cheese 1L is highest by quantity.';
    const rows = [
      { item_name: "Public Cheese 1L", total_quantity_sold: 640, total_sales_amount: 121656.01 },
      { item_name: "Source Milk 1kg", total_quantity_sold: 562, total_sales_amount: 127442.79 },
    ];

    const out = formatAnswer(answer, rows);
    const lines = out.split("\n");

    // Lead first, without the label.
    expect(out.startsWith("Public Cheese 1L is the top seller.")).toBe(true);
    // Bullets in the middle, one per row, with humanized keys. Number grouping
    // is locale-dependent, so assert on the label, keys, and decimals only.
    expect(out).toContain(
      "- **Public Cheese 1L** — total quantity sold: 640, total sales amount:",
    );
    expect(out).toContain(
      "- **Source Milk 1kg** — total quantity sold: 562, total sales amount:",
    );
    expect(out).toMatch(/total sales amount: [\d,]+\.01/);
    expect(out).toMatch(/total sales amount: [\d,]+\.79/);
    expect(lines.filter((l) => l.startsWith("- "))).toHaveLength(2);
    // Conclusion last, kept with a bold label.
    expect(out.trimEnd().endsWith("**Conclusion:** Public Cheese 1L is highest by quantity.")).toBe(true);
  });

  it("omits the bullet list when there are no rows", () => {
    const answer = "Summary: No matching sales were found. Results: [] Conclusion: Nothing to report.";
    const out = formatAnswer(answer, []);
    expect(out).not.toContain("\n- ");
    expect(out).toContain("No matching sales were found.");
    expect(out).toContain("**Conclusion:** Nothing to report.");
  });

  it("uses a plain label bullet when a row has a single field", () => {
    const answer = "Summary: One region. Results: [...] Conclusion: Done.";
    const out = formatAnswer(answer, [{ region: "North" }]);
    // Single-field rows render as just the value, no ' — ' separator.
    expect(out).toContain("- North");
    expect(out).not.toContain("- **North**");
  });

  it("caps the bullet list and notes how many more rows exist", () => {
    const rows = Array.from({ length: 25 }, (_, i) => ({ id: `row-${i}`, n: i }));
    const answer = "Summary: Many rows. Results: [...] Conclusion: End.";
    const out = formatAnswer(answer, rows);
    const bulletLines = out.split("\n").filter((l) => l.startsWith("- "));
    // 20 data bullets + 1 "and N more" line.
    expect(bulletLines).toHaveLength(21);
    expect(out).toContain("- …and 5 more");
  });

  it("handles a template without a Summary label (results/conclusion only)", () => {
    const answer = "These are the figures. Results: [...] Conclusion: That is all.";
    const out = formatAnswer(answer, [{ item: "A", value: 1 }]);
    expect(out.startsWith("These are the figures.")).toBe(true);
    expect(out).toContain("- **A** — value: 1");
    expect(out).toContain("**Conclusion:** That is all.");
    expect(out).not.toMatch(/results\s*:/i);
  });
});
