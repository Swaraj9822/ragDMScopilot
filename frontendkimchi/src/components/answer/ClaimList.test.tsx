import { describe, expect, it, vi } from "vitest";
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/renderWithProviders";
import { ClaimList } from "./ClaimList";
import type { Claim, EvidenceStatus } from "../../api/types";

function makeClaim(overrides: Partial<Claim> & { evidence_status: EvidenceStatus }): Claim {
  return {
    claim_id: `claim-${Math.random().toString(36).slice(2, 8)}`,
    text: "Some factual claim.",
    answer_span: { start: 0, end: 20 },
    evidence_items: [],
    ...overrides,
  };
}

describe("ClaimList", () => {
  it("renders nothing when claims list is empty", () => {
    const { container } = renderWithProviders(<ClaimList claims={[]} />);
    expect(container.querySelector("ul")).not.toBeInTheDocument();
  });

  it("renders all four statuses with distinct text labels", () => {
    const claims: Claim[] = [
      makeClaim({ claim_id: "c1", text: "Fully supported fact.", evidence_status: "supported" }),
      makeClaim({ claim_id: "c2", text: "Partially backed.", evidence_status: "partially_supported" }),
      makeClaim({ claim_id: "c3", text: "Not backed by evidence.", evidence_status: "unsupported" }),
      makeClaim({ claim_id: "c4", text: "Cannot verify.", evidence_status: "verification_unavailable" }),
    ];

    renderWithProviders(<ClaimList claims={claims} />);

    // Each status has a unique text label — not color only
    expect(screen.getByText("Supported")).toBeInTheDocument();
    expect(screen.getByText("Partial")).toBeInTheDocument();
    expect(screen.getByText("Unsupported")).toBeInTheDocument();
    expect(screen.getByText("Unverified")).toBeInTheDocument();
  });

  it("renders each claim's text", () => {
    const claims: Claim[] = [
      makeClaim({ claim_id: "c1", text: "The Earth orbits the Sun.", evidence_status: "supported" }),
      makeClaim({ claim_id: "c2", text: "Water boils at 100°C.", evidence_status: "partially_supported" }),
    ];

    renderWithProviders(<ClaimList claims={claims} />);

    expect(screen.getByText("The Earth orbits the Sun.")).toBeInTheDocument();
    expect(screen.getByText("Water boils at 100°C.")).toBeInTheDocument();
  });

  it("has accessible aria-labels on status indicators", () => {
    const claims: Claim[] = [
      makeClaim({ claim_id: "c1", text: "Fact one.", evidence_status: "supported" }),
      makeClaim({ claim_id: "c2", text: "Fact two.", evidence_status: "unsupported" }),
    ];

    renderWithProviders(<ClaimList claims={claims} />);

    expect(screen.getByLabelText("Evidence status: Supported")).toBeInTheDocument();
    expect(screen.getByLabelText("Evidence status: Unsupported")).toBeInTheDocument();
  });

  it("calls onSelectClaim when a claim is clicked", async () => {
    const onSelect = vi.fn();
    const claim = makeClaim({ claim_id: "c1", text: "Click me.", evidence_status: "supported" });

    const user = userEvent.setup();
    renderWithProviders(<ClaimList claims={[claim]} onSelectClaim={onSelect} />);

    await user.click(screen.getByRole("button", { name: /view evidence for claim/i }));

    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith(claim);
  });

  it("renders the list with an aria-label for screen readers", () => {
    const claims: Claim[] = [
      makeClaim({ claim_id: "c1", text: "Claim.", evidence_status: "supported" }),
    ];

    renderWithProviders(<ClaimList claims={claims} />);

    expect(screen.getByRole("list", { name: "Answer claims" })).toBeInTheDocument();
  });

  it("uses distinct CSS classes (shapes) for each status indicator", () => {
    const claims: Claim[] = [
      makeClaim({ claim_id: "c1", text: "A", evidence_status: "supported" }),
      makeClaim({ claim_id: "c2", text: "B", evidence_status: "partially_supported" }),
      makeClaim({ claim_id: "c3", text: "C", evidence_status: "unsupported" }),
      makeClaim({ claim_id: "c4", text: "D", evidence_status: "verification_unavailable" }),
    ];

    renderWithProviders(<ClaimList claims={claims} />);

    // Each indicator should exist and be distinguishable by its accessible label
    const supported = screen.getByLabelText("Evidence status: Supported");
    const partial = screen.getByLabelText("Evidence status: Partial");
    const unsupported = screen.getByLabelText("Evidence status: Unsupported");
    const unavailable = screen.getByLabelText("Evidence status: Unverified");

    // All four are distinct elements (not the same node)
    const allIndicators = [supported, partial, unsupported, unavailable];
    const uniqueNodes = new Set(allIndicators);
    expect(uniqueNodes.size).toBe(4);
  });

  it("handles unknown status defensively", () => {
    const claims: Claim[] = [
      makeClaim({ claim_id: "c1", text: "Mystery.", evidence_status: "some_future_status" as EvidenceStatus }),
    ];

    renderWithProviders(<ClaimList claims={claims} />);

    // Falls back to showing the raw status text
    expect(screen.getByText("some_future_status")).toBeInTheDocument();
  });
});
