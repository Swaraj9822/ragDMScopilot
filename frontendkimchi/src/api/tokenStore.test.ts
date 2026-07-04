import { beforeEach, describe, expect, it, vi } from "vitest";

import { LOCALSTORAGE_KEYS } from "../lib/constants";

describe("tokenStore", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("keeps the access token in memory only, never in localStorage", async () => {
    const store = await import("./tokenStore");
    store.setAccessToken("access-abc");

    expect(store.getAccessToken()).toBe("access-abc");
    expect(store.hasSession()).toBe(true);
    // Access token is memory-only, so an XSS payload cannot read it from
    // storage; neither token is ever written to JS-readable storage.
    expect(localStorage.getItem(LOCALSTORAGE_KEYS.authAccessToken)).toBeNull();
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

  it("purges any tokens left in localStorage by an older build", async () => {
    // Simulate a legacy build that persisted both tokens, then (re)load the
    // module so its top-level migration purge runs.
    localStorage.setItem(LOCALSTORAGE_KEYS.authAccessToken, "legacy-access");
    localStorage.setItem(LOCALSTORAGE_KEYS.authRefreshToken, "legacy-refresh");
    vi.resetModules();
    await import("./tokenStore");

    expect(localStorage.getItem(LOCALSTORAGE_KEYS.authAccessToken)).toBeNull();
    expect(localStorage.getItem(LOCALSTORAGE_KEYS.authRefreshToken)).toBeNull();
  });
});
