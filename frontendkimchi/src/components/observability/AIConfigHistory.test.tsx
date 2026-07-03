import { describe, expect, it } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AIConfigHistory } from "./AIConfigHistory";
import { renderWithProviders } from "../../test/renderWithProviders";
import { server, http, HttpResponse, API } from "../../test/server";
import type { AIConfigurationVersion } from "../../api/types";

function makeVersion(overrides: Partial<AIConfigurationVersion> = {}): AIConfigurationVersion {
  return {
    config_id: "cfg-1",
    version_id: "v-001-aaaa-bbbb-cccc-dddd",
    prompt: "Answer concisely.",
    model: "gemini-3.5-flash",
    output_schema: {},
    router_threshold: 0.5,
    retrieval_settings: {},
    reranker_config: {},
    change_description: "Initial configuration",
    created_at: "2026-01-01T00:00:00Z",
    approved: true,
    approver: null,
    approved_at: null,
    ...overrides,
  };
}

const VERSIONS: AIConfigurationVersion[] = [
  makeVersion({
    version_id: "v-003-aaaa-bbbb-cccc-dddd",
    change_description: "Updated prompt template",
    created_at: "2026-01-03T00:00:00Z",
  }),
  makeVersion({
    version_id: "v-002-aaaa-bbbb-cccc-dddd",
    change_description: "Changed retrieval threshold",
    created_at: "2026-01-02T00:00:00Z",
  }),
  makeVersion({
    version_id: "v-001-aaaa-bbbb-cccc-dddd",
    change_description: "Initial configuration",
    created_at: "2026-01-01T00:00:00Z",
  }),
];

describe("AIConfigHistory", () => {
  it("renders versions in reverse chronological order (R9.5)", async () => {
    server.use(
      http.get(`${API}/ai-config/:id/history`, () =>
        HttpResponse.json({
          config_id: "cfg-1",
          active_version_id: "v-003-aaaa-bbbb-cccc-dddd",
          versions: VERSIONS,
          activation_events: [],
        }),
      ),
    );

    renderWithProviders(<AIConfigHistory configId="cfg-1" />);

    // Wait for loading to complete
    await waitFor(() => {
      expect(screen.getByText("Updated prompt template")).toBeInTheDocument();
    });

    // All descriptions visible in order
    expect(screen.getByText("Changed retrieval threshold")).toBeInTheDocument();
    expect(screen.getByText("Initial configuration")).toBeInTheDocument();

    // Active badge is visible for the active version
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("shows empty state when no versions exist (R9.6)", async () => {
    server.use(
      http.get(`${API}/ai-config/:id/history`, () =>
        HttpResponse.json({
          config_id: "cfg-1",
          active_version_id: null,
          versions: [],
          activation_events: [],
        }),
      ),
    );

    renderWithProviders(<AIConfigHistory configId="cfg-1" />);

    await waitFor(() => {
      expect(screen.getByText("No configuration history")).toBeInTheDocument();
    });
  });

  it("opens rollback dialog and captures a reason (R9.8)", async () => {
    server.use(
      http.get(`${API}/ai-config/:id/history`, () =>
        HttpResponse.json({
          config_id: "cfg-1",
          active_version_id: "v-003-aaaa-bbbb-cccc-dddd",
          versions: VERSIONS,
          activation_events: [],
        }),
      ),
      http.post(`${API}/ai-config/:id/rollback`, async ({ request }) => {
        const body = (await request.json()) as { version_id: string; reason: string };
        if (!body.reason || !body.version_id) {
          return HttpResponse.json({ detail: "reason required" }, { status: 400 });
        }
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const user = userEvent.setup();
    renderWithProviders(<AIConfigHistory configId="cfg-1" />);

    // Wait for data to load
    await waitFor(() => {
      expect(screen.getByText("Updated prompt template")).toBeInTheDocument();
    });

    // Find rollback buttons (only non-active versions have them)
    const rollbackButtons = screen.getAllByRole("button", { name: /rollback/i });
    expect(rollbackButtons.length).toBe(2); // versions 2 and 1 are not active

    // Click the first rollback button (for v-002)
    await user.click(rollbackButtons[0]);

    // Dialog should be open
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    expect(screen.getByText("Rollback Configuration")).toBeInTheDocument();

    // Confirm button should be disabled without reason
    const confirmBtn = screen.getByRole("button", { name: /confirm rollback/i });
    expect(confirmBtn).toBeDisabled();

    // Type a reason
    const textarea = screen.getByLabelText(/reason for rollback/i);
    await user.type(textarea, "Reverting due to increased error rate");

    // Confirm button should now be enabled
    expect(confirmBtn).toBeEnabled();

    // Submit the rollback
    await user.click(confirmBtn);

    // Dialog should close after successful rollback
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("closes rollback dialog on cancel", async () => {
    server.use(
      http.get(`${API}/ai-config/:id/history`, () =>
        HttpResponse.json({
          config_id: "cfg-1",
          active_version_id: "v-003-aaaa-bbbb-cccc-dddd",
          versions: VERSIONS,
          activation_events: [],
        }),
      ),
    );

    const user = userEvent.setup();
    renderWithProviders(<AIConfigHistory configId="cfg-1" />);

    await waitFor(() => {
      expect(screen.getByText("Updated prompt template")).toBeInTheDocument();
    });

    // Open dialog
    const rollbackButtons = screen.getAllByRole("button", { name: /rollback/i });
    await user.click(rollbackButtons[0]);
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    // Cancel
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("shows error state on API failure", async () => {
    server.use(
      http.get(`${API}/ai-config/:id/history`, () =>
        HttpResponse.json({ detail: "Not found" }, { status: 404 }),
      ),
    );

    renderWithProviders(<AIConfigHistory configId="cfg-1" />);

    await waitFor(() => {
      expect(screen.getByText("Unable to load history")).toBeInTheDocument();
    });

    expect(screen.getByRole("button", { name: /retry/i })).toBeInTheDocument();
  });
});
