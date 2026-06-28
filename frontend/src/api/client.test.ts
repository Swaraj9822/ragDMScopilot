import { describe, expect, it } from "vitest";
import { apiClient, ApiError, newTraceId } from "./client";
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
});

describe("newTraceId", () => {
  it("produces a 32-char lowercase hex string", () => {
    const id = newTraceId();
    expect(id).toMatch(/^[0-9a-f]{32}$/);
  });
});
