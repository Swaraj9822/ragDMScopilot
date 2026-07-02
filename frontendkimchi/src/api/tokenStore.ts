// Low-level access-token storage. Kept dependency-free (no imports from the API
// client) so client.ts can read the token without a circular import.
//
// The access token lives in memory and is mirrored to localStorage so a page
// reload keeps the session; it is short-lived (backend default 60 min).
//
// SECURITY: the refresh token is NOT stored here. It is delivered as an
// httpOnly cookie the browser manages, so page JavaScript — and therefore any
// XSS payload — cannot read it. A refresh is performed by calling
// /auth/refresh with credentials; the browser attaches the cookie automatically.

import { LOCALSTORAGE_KEYS } from "../lib/constants";

type Listener = () => void;

let accessToken: string | null = readStored(LOCALSTORAGE_KEYS.authAccessToken);
const listeners = new Set<Listener>();

// Migration/hardening: purge any refresh token a previous build persisted to
// localStorage so a formerly-stored token cannot linger in JS-readable storage.
try {
  localStorage.removeItem(LOCALSTORAGE_KEYS.authRefreshToken);
} catch {
  // Storage unavailable (private mode / SSR) — nothing to purge.
}

function readStored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string | null): void {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    // Storage unavailable (private mode / quota) — fall back to in-memory only.
  }
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

/** Persist a freshly issued access token and notify subscribers. */
export function setAccessToken(access: string): void {
  accessToken = access;
  writeStored(LOCALSTORAGE_KEYS.authAccessToken, access);
  notify();
}

/** Drop the session (logout or unrecoverable auth failure) and notify subscribers. */
export function clearTokens(): void {
  const had = accessToken !== null;
  accessToken = null;
  writeStored(LOCALSTORAGE_KEYS.authAccessToken, null);
  if (had) notify();
}

/** Subscribe to token changes (login / logout / refresh). Returns an unsubscribe. */
export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
