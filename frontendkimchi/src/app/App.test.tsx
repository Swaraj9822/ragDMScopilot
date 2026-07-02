import { describe, expect, it } from "vitest";
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { App } from "./App";
import { renderWithProviders } from "../test/renderWithProviders";

describe("App navigation", () => {
  it("redirects / to the Copilot tab", async () => {
    renderWithProviders(<App />, { route: "/" });
    expect(
      await screen.findByRole("heading", {
        name: /ask across documents and business data/i,
      }),
    ).toBeInTheDocument();
  });

  it("starts on a clean Copilot tab after authenticating, ignoring the entry URL", async () => {
    // Simulates opening the app (resumed session) while the address bar still
    // points at a previously-visited Observability view.
    renderWithProviders(<App />, { route: "/observability?view=queries" });
    expect(
      await screen.findByRole("heading", {
        name: /ask across documents and business data/i,
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /ai observability/i }),
    ).not.toBeInTheDocument();
  });

  it("exposes three top-level tabs and marks the active one", async () => {
    renderWithProviders(<App />, { route: "/copilot" });
    await screen.findByRole("heading", {
      name: /ask across documents and business data/i,
    });
    const nav = screen.getByRole("navigation", { name: /primary/i });
    const copilotTab = within(nav).getByRole("link", { name: /copilot/i });
    expect(copilotTab).toHaveAttribute("aria-current", "page");
    expect(within(nav).getByRole("link", { name: /ai observability/i })).toBeInTheDocument();
    expect(within(nav).getByRole("link", { name: /documents/i })).toBeInTheDocument();
  });

  it("navigates to Observability when its tab is clicked", async () => {
    const user = userEvent.setup();
    renderWithProviders(<App />, { route: "/copilot" });
    await user.click(await screen.findByRole("link", { name: /ai observability/i }));
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: /ai observability/i }),
      ).toBeInTheDocument(),
    );
  });
});
