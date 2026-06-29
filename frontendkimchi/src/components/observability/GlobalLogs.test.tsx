import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GlobalLogs } from "./GlobalLogs";
import { renderWithProviders } from "../../test/renderWithProviders";

describe("GlobalLogs", () => {
  it("disables Search when the date range is inverted", async () => {
    const user = userEvent.setup();
    renderWithProviders(<GlobalLogs onTraceClick={() => {}} />);

    const start = screen.getByLabelText(/start/i);
    const end = screen.getByLabelText(/end/i);
    await user.type(start, "2026-01-02T00:00");
    await user.type(end, "2026-01-01T00:00");

    expect(screen.getByRole("button", { name: /search/i })).toBeDisabled();
    expect(screen.getByText(/end must not be earlier than start/i)).toBeInTheDocument();
  });

  it("keeps Search enabled for a valid range", async () => {
    const user = userEvent.setup();
    renderWithProviders(<GlobalLogs onTraceClick={() => {}} />);

    await user.type(screen.getByLabelText(/start/i), "2026-01-01T00:00");
    await user.type(screen.getByLabelText(/end/i), "2026-01-02T00:00");

    expect(screen.getByRole("button", { name: /search/i })).toBeEnabled();
  });
});
