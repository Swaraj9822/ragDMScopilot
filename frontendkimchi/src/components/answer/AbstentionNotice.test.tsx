import { describe, expect, it } from "vitest";
import { screen } from "@testing-library/react";
import { renderWithProviders } from "../../test/renderWithProviders";
import { AbstentionNotice } from "./AbstentionNotice";
import type { AbstentionResponse, ReasonCode } from "../../api/types";

function makeAbstention(overrides: Partial<AbstentionResponse> = {}): AbstentionResponse {
  return {
    reason_code: "no_evidence",
    missing_information: "No documents in the corpus discuss quantum entanglement.",
    trace_id: "trace-abc-123",
    ...overrides,
  };
}

describe("AbstentionNotice", () => {
  it("displays missing_information description (R3.9)", () => {
    const response = makeAbstention({
      missing_information: "The corpus lacks data about recent election results.",
    });

    renderWithProviders(<AbstentionNotice response={response} />);

    expect(
      screen.getByText("The corpus lacks data about recent election results."),
    ).toBeInTheDocument();
  });

  it("surfaces the reason_code for operator transparency", () => {
    const response = makeAbstention({ reason_code: "conflicting_evidence" });

    renderWithProviders(<AbstentionNotice response={response} />);

    expect(screen.getByText("Conflicting evidence")).toBeInTheDocument();
  });

  it("never displays an answer", () => {
    const response = makeAbstention();

    const { container } = renderWithProviders(<AbstentionNotice response={response} />);

    // The component renders only the notice article — no answer text or claim elements
    const article = container.querySelector("article");
    expect(article).toBeInTheDocument();
    // Confirm no "answer" content appears (component only shows description + reason)
    expect(screen.queryByRole("heading", { name: /answer/i })).not.toBeInTheDocument();
  });

  it("shows default insufficient-evidence notice when missing_information is empty (R3.10)", () => {
    const response = makeAbstention({ missing_information: "" });

    renderWithProviders(<AbstentionNotice response={response} />);

    expect(
      screen.getByText(
        "There is not enough evidence in the available sources to answer this question reliably.",
      ),
    ).toBeInTheDocument();
  });

  it("shows default notice when missing_information is whitespace-only (R3.10)", () => {
    const response = makeAbstention({ missing_information: "   " });

    renderWithProviders(<AbstentionNotice response={response} />);

    expect(
      screen.getByText(
        "There is not enough evidence in the available sources to answer this question reliably.",
      ),
    ).toBeInTheDocument();
  });

  it("maps all six reason codes to human-friendly labels", () => {
    const codes: { code: ReasonCode; label: string }[] = [
      { code: "low_confidence", label: "Low confidence" },
      { code: "no_evidence", label: "No evidence found" },
      { code: "unsupported_claims", label: "Unsupported claims" },
      { code: "conflicting_evidence", label: "Conflicting evidence" },
      { code: "sql_no_rows", label: "No matching rows" },
      { code: "retrieval_below_threshold", label: "Retrieval below threshold" },
    ];

    for (const { code, label } of codes) {
      const response = makeAbstention({ reason_code: code });
      const { unmount } = renderWithProviders(<AbstentionNotice response={response} />);
      expect(screen.getByText(label)).toBeInTheDocument();
      unmount();
    }
  });

  it("handles an unknown reason_code defensively", () => {
    const response = makeAbstention({ reason_code: "future_reason" });

    renderWithProviders(<AbstentionNotice response={response} />);

    // Falls back to showing the raw code
    expect(screen.getByText("future_reason")).toBeInTheDocument();
  });

  it("has an accessible aria-label", () => {
    renderWithProviders(<AbstentionNotice response={makeAbstention()} />);

    expect(
      screen.getByRole("alert", { name: "The system could not provide an answer" }),
    ).toBeInTheDocument();
  });

  it("renders the 'Unable to answer' title", () => {
    renderWithProviders(<AbstentionNotice response={makeAbstention()} />);

    expect(screen.getByText("Unable to answer")).toBeInTheDocument();
  });
});
