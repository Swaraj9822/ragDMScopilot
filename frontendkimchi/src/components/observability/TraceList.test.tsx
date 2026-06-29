import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { TraceList } from "./TraceList";
import type { Trace } from "../../api/types";

const traces: Trace[] = [
  {
    trace_id: "b".repeat(32),
    route: "/ask",
    start_ts: "2026-01-01T00:00:00.000Z",
    duration_ms: 842,
    root_status: "success",
    spans: [{ span_id: "s1", parent_span_id: null, operation: "op", start_ts: "2026-01-01T00:00:00.000Z", duration_ms: 842, status: "success", attributes: {} }],
  },
];

describe("TraceList", () => {
  it("activates a row with the Enter key", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<TraceList traces={traces} selectedId={null} onSelect={onSelect} />);

    const row = screen.getByRole("row", { selected: false });
    row.focus();
    await user.keyboard("{Enter}");
    expect(onSelect).toHaveBeenCalledWith("b".repeat(32));
  });

  it("activates a row with a click", async () => {
    const onSelect = vi.fn();
    const user = userEvent.setup();
    render(<TraceList traces={traces} selectedId={null} onSelect={onSelect} />);
    await user.click(screen.getByText("/ask"));
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it("marks the selected row via aria-selected", () => {
    render(<TraceList traces={traces} selectedId={"b".repeat(32)} onSelect={() => {}} />);
    expect(screen.getByRole("row", { selected: true })).toBeInTheDocument();
  });
});
