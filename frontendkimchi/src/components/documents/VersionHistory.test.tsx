import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { renderWithProviders } from "../../test/renderWithProviders";
import { server, http, HttpResponse, API } from "../../test/server";
import { VersionHistory } from "./VersionHistory";
import type { DocumentHistory } from "../../api/types";

const DOC_ID = "doc-123";

const sampleHistory: DocumentHistory = {
  document_id: DOC_ID,
  active_version: "v2",
  versions: [
    {
      document_id: DOC_ID,
      version: "v2",
      created_at: "2024-06-15T10:00:00Z",
      indexed: true,
      vectors_present: true,
      source_retained: true,
    },
    {
      document_id: DOC_ID,
      version: "v1",
      created_at: "2024-06-10T08:00:00Z",
      indexed: true,
      vectors_present: true,
      source_retained: true,
    },
  ],
  events: [
    {
      ingestion_id: "ing-2",
      document_id: DOC_ID,
      version: "v2",
      status: "succeeded",
      timestamp: "2024-06-15T10:00:00Z",
      error: null,
    },
    {
      ingestion_id: "ing-fail",
      document_id: DOC_ID,
      version: "v1-retry",
      status: "failed",
      timestamp: "2024-06-12T09:00:00Z",
      error: "Embedding service unavailable",
    },
    {
      ingestion_id: "ing-1",
      document_id: DOC_ID,
      version: "v1",
      status: "succeeded",
      timestamp: "2024-06-10T08:00:00Z",
      error: null,
    },
  ],
};

function setupHistoryHandler(history: DocumentHistory = sampleHistory) {
  server.use(
    http.get(`${API}/documents/${DOC_ID}/versions`, () =>
      HttpResponse.json(history),
    ),
  );
}

function setupOperatorUser() {
  server.use(
    http.get(`${API}/auth/me`, () =>
      HttpResponse.json({
        id: "operator-user",
        email: "op@example.com",
        is_active: true,
        created_at: "2024-01-01T00:00:00Z",
        is_operator: true,
      }),
    ),
  );
}

describe("VersionHistory", () => {
  it("renders versions and events newest-first", async () => {
    setupHistoryHandler();
    renderWithProviders(<VersionHistory documentId={DOC_ID} />);

    await waitFor(() => {
      expect(screen.getByText("Version v2")).toBeInTheDocument();
    });

    expect(screen.getByText("Version v1")).toBeInTheDocument();
    expect(screen.getByText("Ingestion v2")).toBeInTheDocument();
    expect(screen.getByText("Ingestion v1")).toBeInTheDocument();
  });

  it("marks the active version with a badge", async () => {
    setupHistoryHandler();
    renderWithProviders(<VersionHistory documentId={DOC_ID} />);

    await waitFor(() => {
      expect(screen.getByText("Active")).toBeInTheDocument();
    });
  });

  it("shows failed ingestion error message", async () => {
    setupHistoryHandler();
    renderWithProviders(<VersionHistory documentId={DOC_ID} />);

    await waitFor(() => {
      expect(screen.getByText("Embedding service unavailable")).toBeInTheDocument();
    });
  });

  it("does not show restore buttons for non-operators", async () => {
    setupHistoryHandler();
    // Default user in test server is non-operator
    renderWithProviders(<VersionHistory documentId={DOC_ID} />);

    await waitFor(() => {
      expect(screen.getByText("Version v1")).toBeInTheDocument();
    });

    expect(screen.queryByRole("button", { name: /restore/i })).not.toBeInTheDocument();
  });

  it("shows restore button for operators on non-active versions", async () => {
    setupOperatorUser();
    setupHistoryHandler();
    renderWithProviders(<VersionHistory documentId={DOC_ID} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /restore version v1/i })).toBeInTheDocument();
    });

    // No restore for the active version
    expect(screen.queryByRole("button", { name: /restore version v2/i })).not.toBeInTheDocument();
  });

  it("shows confirmation dialog before restoring", async () => {
    setupOperatorUser();
    setupHistoryHandler();
    server.use(
      http.post(`${API}/documents/${DOC_ID}/versions/v1/restore`, () =>
        new HttpResponse(null, { status: 204 }),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<VersionHistory documentId={DOC_ID} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /restore version v1/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /restore version v1/i }));

    // Confirmation dialog appears
    expect(screen.getByRole("alertdialog")).toBeInTheDocument();
    expect(screen.getByText("Restore version?")).toBeInTheDocument();
    expect(screen.getByText(/set version "v1" as the active version/)).toBeInTheDocument();
  });

  it("handles the 404 version_not_found error on restore", async () => {
    setupOperatorUser();
    setupHistoryHandler();
    server.use(
      http.post(`${API}/documents/${DOC_ID}/versions/v1/restore`, () =>
        HttpResponse.json({ detail: "version_not_found" }, { status: 404 }),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<VersionHistory documentId={DOC_ID} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /restore version v1/i })).toBeInTheDocument();
    });

    await user.click(screen.getByRole("button", { name: /restore version v1/i }));
    // Confirm in the dialog
    await user.click(screen.getByRole("button", { name: /^Restore$/i }));

    await waitFor(() => {
      expect(screen.getByRole("alert")).toBeInTheDocument();
      expect(screen.getByText(/version not found/i)).toBeInTheDocument();
    });
  });

  it("shows empty state when no history exists", async () => {
    setupHistoryHandler({
      document_id: DOC_ID,
      active_version: null,
      versions: [],
      events: [],
    });
    renderWithProviders(<VersionHistory documentId={DOC_ID} />);

    await waitFor(() => {
      expect(screen.getByText("No version history")).toBeInTheDocument();
    });
  });

  it("shows error state when fetch fails", async () => {
    server.use(
      http.get(`${API}/documents/${DOC_ID}/versions`, () =>
        HttpResponse.json({ detail: "Internal server error" }, { status: 500 }),
      ),
    );
    renderWithProviders(<VersionHistory documentId={DOC_ID} />);

    await waitFor(() => {
      expect(screen.getByText("Failed to load version history")).toBeInTheDocument();
    });
  });
});
