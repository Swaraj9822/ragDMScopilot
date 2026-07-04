// Low-level access-token storage. Kept dependency-free (no imports from the API
// client) so client.ts can read the token without a circular import.
//
// The access token lives in memory ONLY. It is never mirrored to localStorage,
// so an XSS payload cannot read it out of storage. A page reload therefore
// starts with no access token; the session is restored by a silent
// /auth/refresh (see AuthProvider), which uses the httpOnly refresh cookie the
// browser still holds. The one cost is a single refresh round-trip on reload.
//
// SECURITY: the refresh token is NOT stored here either. It is delivered as an
// httpOnly cookie the browser manages, so page JavaScript — and therefore any
// XSS payload — cannot read it.

import { LOCALSTORAGE_KEYS } from "../lib/constants";

type Listener = () => void;

let accessToken: string | null = null;
const listeners = new Set<Listener>();

// Migration/hardening: purge any tokens a previous build persisted to
// localStorage. The access token is now memory-only and the refresh token lives
// only in an httpOnly cookie, so neither should ever sit in JS-readable storage.
try {
  localStorage.removeItem(LOCALSTORAGE_KEYS.authAccessToken);
  localStorage.removeItem(LOCALSTORAGE_KEYS.authRefreshToken);
} catch {
  // Storage unavailable (private mode / SSR) — nothing to purge.
}

function notify(): void {
  for (const listener of listeners) listener();
}

export function getAccessToken(): string | null {
  return accessToken;
}

export function hasSession(): boolean {
  return accessToken !== null;
}

/** Persist a freshly issued access token (in memory) and notify subscribers. */
export function setAccessToken(access: string): void {
  accessToken = access;
  notify();
}

/** Drop the session (logout or unrecoverable auth failure) and notify subscribers. */
export function clearTokens(): void {
  const had = accessToken !== null;
  accessToken = null;
  if (had) notify();
}

/** Subscribe to token changes (login / logout / refresh). Returns an unsubscribe. */
export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
