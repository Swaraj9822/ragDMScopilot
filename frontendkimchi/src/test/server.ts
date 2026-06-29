import { setupServer } from "msw/node";
import { http, HttpResponse } from "msw";

export const API = "http://localhost:8000";

// Default handlers represent a healthy backend with empty observability data.
export const defaultHandlers = [
  http.get(`${API}/health`, () => HttpResponse.json({ status: "ok" })),
  http.get(`${API}/traces`, () => HttpResponse.json([])),
  http.get(`${API}/logs`, () => HttpResponse.json([])),
];

export const server = setupServer(...defaultHandlers);

export { http, HttpResponse };
