import { beforeEach, describe, expect, it, vi } from "vitest";

import { LOCALSTORAGE_KEYS } from "../lib/constants";

describe("tokenStore", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("persists the access token but never a refresh token", async () => {
    const store = await import("./tokenStore");
    store.setAccessToken("access-abc");

    expect(store.getAccessToken()).toBe("access-abc");
    expect(store.hasSession()).toBe(true);
    // Access token mirrored to localStorage for reload persistence...
    expect(localStorage.getItem(LOCALSTORAGE_KEYS.authAccessToken)).toBe("access-abc");
    // ...but the refresh token is never written to JS-readable storage.
    expect(localStorage.getItem(LOCALSTORAGE_KEYS.authRefreshToken)).toBeNull();
  });

  it("clearTokens drops the access token and session", async () => {
    const store = await import("./tokenStore");
    store.setAccessToken("access-abc");
    store.clearTokens();

    expect(store.getAccessToken()).toBeNull();
    expect(store.hasSession()).toBe(false);
    expect(localStorage.getItem(LOCALSTORAGE_KEYS.authAccessToken)).toBeNull();
  });

  it("purges any refresh token left in localStorage by an older build", async () => {
    // Simulate a legacy persisted refresh token, then (re)load the module so
    // its top-level migration runs.
    localStorage.setItem(LOCALSTORAGE_KEYS.authRefreshToken, "legacy-refresh");
    vi.resetModules();
    await import("./tokenStore");

    expect(localStorage.getItem(LOCALSTORAGE_KEYS.authRefreshToken)).toBeNull();
  });
});
