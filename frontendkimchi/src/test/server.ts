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
  // Login/refresh return only the access token in the body; the refresh token
  // is delivered as an httpOnly cookie (not observable from JS, mirrored here
  // via Set-Cookie so the flow matches production).
  http.post(`${API}/auth/login`, () =>
    HttpResponse.json(
      { access_token: "test-access-token", token_type: "bearer", expires_in: 3600 },
      { headers: { "Set-Cookie": "refresh_token=test-refresh; Path=/auth; HttpOnly" } },
    ),
  ),
  http.post(`${API}/auth/refresh`, () =>
    HttpResponse.json(
      { access_token: "refreshed-access-token", token_type: "bearer", expires_in: 3600 },
      { headers: { "Set-Cookie": "refresh_token=test-refresh-2; Path=/auth; HttpOnly" } },
    ),
  ),
  http.post(`${API}/auth/logout`, () => new HttpResponse(null, { status: 204 })),
];

export const server = setupServer(...defaultHandlers);

export { http, HttpResponse };
