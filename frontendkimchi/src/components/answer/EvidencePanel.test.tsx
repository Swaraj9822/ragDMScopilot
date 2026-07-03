import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { EvidencePanel } from "./EvidencePanel";
import type { Claim, DocumentEvidenceItem, DatabaseEvidenceItem } from "../../api/types";

function makeClaim(overrides: Partial<Claim> = {}): Claim {
  return {
    claim_id: "claim-001",
    text: "Total revenue was $1.2M in Q4.",
    answer_span: { start: 0, end: 30 },
    evidence_items: [],
    evidence_status: "supported",
    ...overrides,
  };
}

function makeDocEvidence(overrides: Partial<DocumentEvidenceItem> = {}): DocumentEvidenceItem {
  return {
    kind: "document",
    quote: "The Q4 revenue totaled $1.2 million.",
    source_start: 100,
    source_end: 136,
    document_id: "doc-abc-123-456-789",
    document_version: "ver-xyz-987-654-321",
    verification_result: "entails",
    coverage: "full",
    covered_subclaims: [],
    ...overrides,
  };
}

function makeDbEvidence(overrides: Partial<DatabaseEvidenceItem> = {}): DatabaseEvidenceItem {
  return {
    kind: "database",
    table: "quarterly_revenue",
    row_fields: { quarter: "Q4", revenue: 1200000, year: 2024 },
    sql: "SELECT * FROM quarterly_revenue WHERE quarter='Q4'",
    sql_query_id: "sql-q1",
    sql_result_fixture_id: null,
    row_index: 0,
    verification_result: "entails",
    coverage: "full",
    covered_subclaims: [],
    ...overrides,
  };
}

describe("EvidencePanel", () => {
  it("renders nothing when claim is null", () => {
    const { container } = render(<EvidencePanel claim={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when claim status is unsupported", () => {
    const claim = makeClaim({ evidence_status: "unsupported" });
    const { container } = render(<EvidencePanel claim={claim} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when claim status is verification_unavailable", () => {
    const claim = makeClaim({ evidence_status: "verification_unavailable" });
    const { container } = render(<EvidencePanel claim={claim} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders evidence for a supported claim with document evidence", () => {
    const claim = makeClaim({
      evidence_status: "supported",
      evidence_items: [makeDocEvidence()],
    });
    render(<EvidencePanel claim={claim} />);

    expect(screen.getByRole("region", { name: /evidence for selected claim/i })).toBeInTheDocument();
    expect(screen.getByText("Evidence (1)")).toBeInTheDocument();
    expect(screen.getByText("The Q4 revenue totaled $1.2 million.")).toBeInTheDocument();
    // Source document and version (shortened)
    expect(screen.getByText(/Source:/)).toBeInTheDocument();
    expect(screen.getByText(/Version:/)).toBeInTheDocument();
  });

  it("renders evidence for a partially_supported claim with database evidence", () => {
    const claim = makeClaim({
      evidence_status: "partially_supported",
      evidence_items: [makeDbEvidence()],
    });
    render(<EvidencePanel claim={claim} />);

    expect(screen.getByRole("region", { name: /evidence for selected claim/i })).toBeInTheDocument();
    expect(screen.getByText("quarterly_revenue")).toBeInTheDocument();
    // Row field values rendered in a table
    expect(screen.getByText("quarter")).toBeInTheDocument();
    expect(screen.getByText("Q4")).toBeInTheDocument();
    expect(screen.getByText("1200000")).toBeInTheDocument();
  });

  it("renders multiple evidence items of mixed kinds", () => {
    const claim = makeClaim({
      evidence_status: "supported",
      evidence_items: [makeDocEvidence(), makeDbEvidence()],
    });
    render(<EvidencePanel claim={claim} />);

    expect(screen.getByText("Evidence (2)")).toBeInTheDocument();
    // Both types present
    expect(screen.getByText("The Q4 revenue totaled $1.2 million.")).toBeInTheDocument();
    expect(screen.getByText("quarterly_revenue")).toBeInTheDocument();
  });

  it("shows empty notice when evidence_items is empty for a supported claim", () => {
    const claim = makeClaim({
      evidence_status: "supported",
      evidence_items: [],
    });
    render(<EvidencePanel claim={claim} />);

    expect(screen.getByText(/no evidence items available/i)).toBeInTheDocument();
  });

  it("shows 'evidence unavailable' notice on render error without crashing", () => {
    // Suppress console.error from the error boundary during this test
    const consoleSpy = vi.spyOn(console, "error").mockImplementation(() => {});

    // Create a claim with malformed evidence that will cause a render error
    const badClaim = makeClaim({
      evidence_status: "supported",
      evidence_items: [
        // Force a render error by providing evidence_items as a non-iterable via casting
        null as unknown as DocumentEvidenceItem,
      ],
    });
    render(<EvidencePanel claim={badClaim} />);

    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(screen.getByText(/evidence unavailable/i)).toBeInTheDocument();

    consoleSpy.mockRestore();
  });
});
