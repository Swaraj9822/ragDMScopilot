import { describe, expect, it } from "vitest";
import { screen, within } from "@testing-library/react";
import { renderWithProviders } from "../../test/renderWithProviders";
import { server, http, HttpResponse, API } from "../../test/server";
import { App } from "../../app/App";

describe("PrimaryNav operator gating", () => {
  it("hides operator-only tabs for non-operator users", async () => {
    // Default mock user has is_operator: false
    renderWithProviders(<App />, { route: "/copilot" });
    const nav = await screen.findByRole("navigation", { name: /primary/i });

    // Public tabs should be visible
    expect(within(nav).getByRole("link", { name: /copilot/i })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /ai observability/i })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /documents/i })).toBeInTheDocument();

    // Operator-only tabs should be hidden
    expect(within(nav).queryByRole("link", { name: /evaluation/i })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: /feedback/i })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: /replay lab/i })).not.toBeInTheDocument();
    expect(within(nav).queryByRole("link", { name: /knowledge gaps/i })).not.toBeInTheDocument();
  });

  it("shows operator-only tabs for operator users", async () => {
    server.use(
      http.get(`${API}/auth/me`, () =>
        HttpResponse.json({
          id: "operator-user",
          email: "operator@example.com",
          is_active: true,
          created_at: "2024-01-01T00:00:00Z",
          is_operator: true,
        }),
      ),
    );

    renderWithProviders(<App />, { route: "/copilot" });
    const nav = await screen.findByRole("navigation", { name: /primary/i });

    // All tabs should be visible for operators
    expect(within(nav).getByRole("link", { name: /copilot/i })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /ai observability/i })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /documents/i })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /evaluation/i })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /feedback/i })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /replay lab/i })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /knowledge gaps/i })).toBeInTheDocument();
  });
});
