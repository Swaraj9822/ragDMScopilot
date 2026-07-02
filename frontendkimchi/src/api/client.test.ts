import { describe, expect, it } from "vitest";
import { apiClient, ApiError, newTraceId } from "./client";
import { getAccessToken, setAccessToken } from "./tokenStore";
import { API, http, HttpResponse, server } from "../test/server";

describe("apiClient", () => {
  it("parses FastAPI { detail } errors into ApiError", async () => {
    server.use(
      http.get(`${API}/documents/x`, () =>
        HttpResponse.json({ detail: "Document not found." }, { status: 404 }),
      ),
    );
    await expect(apiClient.get("/documents/x")).rejects.toMatchObject({
      name: "ApiError",
      status: 404,
      detail: "Document not found.",
    });
  });

  it("falls back to a generic message for non-JSON errors", async () => {
    server.use(
      http.get(`${API}/boom`, () => new HttpResponse("oops", { status: 500 })),
    );
    const error = await apiClient.get("/boom").catch((e) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(500);
  });

  it("treats an empty array response as success", async () => {
    server.use(http.get(`${API}/logs`, () => HttpResponse.json([])));
    await expect(apiClient.get("/logs")).resolves.toEqual([]);
  });

  it("refreshes via the httpOnly cookie on 401 and retries the request", async () => {
    setAccessToken("expired-access-token");
    let attempts = 0;
    server.use(
      http.get(`${API}/documents`, () => {
        attempts += 1;
        if (attempts === 1) {
          return HttpResponse.json({ detail: "expired" }, { status: 401 });
        }
        return HttpResponse.json([{ id: "d1" }]);
      }),
      // No request body/refresh token needed — the browser sends the cookie.
      http.post(`${API}/auth/refresh`, () =>
        HttpResponse.json({
          access_token: "fresh-access-token",
          token_type: "bearer",
          expires_in: 3600,
        }),
      ),
    );

    const result = await apiClient.get("/documents");

    expect(result).toEqual([{ id: "d1" }]);
    expect(getAccessToken()).toBe("fresh-access-token");
    expect(attempts).toBe(2);
  });

  it("clears the session when the refresh cookie is rejected", async () => {
    setAccessToken("expired-access-token");
    server.use(
      http.get(`${API}/secure`, () =>
        HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
      http.post(`${API}/auth/refresh`, () =>
        HttpResponse.json({ detail: "invalid" }, { status: 401 }),
      ),
    );

    await expect(apiClient.get("/secure")).rejects.toBeInstanceOf(ApiError);
    expect(getAccessToken()).toBeNull();
  });
});

describe("newTraceId", () => {
  it("produces a 32-char lowercase hex string", () => {
    const id = newTraceId();
    expect(id).toMatch(/^[0-9a-f]{32}$/);
  });
});
