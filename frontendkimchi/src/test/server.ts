import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";

export const API = "http://localhost:8000";

// Default handlers represent a healthy backend with empty observability data.
export const defaultHandlers = [
  http.get(`${API}/health`, () => HttpResponse.json({ status: "ok" })),
  http.get(`${API}/traces`, () => HttpResponse.json([])),
  http.get(`${API}/logs`, () => HttpResponse.json([])),
  // Auth: resolve the seeded test session to a user (see renderWithProviders).
  http.get(`${API}/auth/me`, () =>
    HttpResponse.json({
      id: "test-user",
      email: "test@example.com",
      is_active: true,
      created_at: "2024-01-01T00:00:00Z",
    }),
  ),
];

export const server = setupServer(...defaultHandlers);

export { http, HttpResponse };
