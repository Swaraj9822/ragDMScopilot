import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { IngestionPipeline } from "./IngestionPipeline";

describe("IngestionPipeline", () => {
  it("shows all five pipeline steps", () => {
    render(<IngestionPipeline status="queued" />);
    for (const step of ["Queued", "Parsing", "Chunking", "Embedding", "Indexed"]) {
      expect(screen.getByText(step)).toBeInTheDocument();
    }
  });

  it("shows the Pinecone completion copy when indexed", () => {
    render(<IngestionPipeline status="indexed" />);
    expect(screen.getByText(/indexed in pinecone/i)).toBeInTheDocument();
  });

  it("renders a neutral terminal state when deleted", () => {
    render(<IngestionPipeline status="deleted" />);
    expect(screen.getByText(/document deleted/i)).toBeInTheDocument();
    expect(screen.queryByText("Queued")).not.toBeInTheDocument();
  });

  it("marks the current step as the failure point", () => {
    const { container } = render(<IngestionPipeline status="failed" />);
    // The failed status maps to the Queued step index (0) as the last known step.
    expect(container.querySelector('[class*="failed"]')).toBeTruthy();
  });
});
