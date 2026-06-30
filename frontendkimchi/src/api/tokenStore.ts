// Low-level access/refresh token storage. Kept dependency-free (no imports from
// the API client) so client.ts can read tokens without a circular import.
//
// Tokens live in memory for speed and are mirrored to localStorage so a page
// reload keeps the session. localStorage is readable by any script on the page,
// so this trades some XSS exposure for persistence — an accepted tradeoff for an
// internal tool. Keep access-token TTLs short (the backend default is 60 min).

import { LOCALSTORAGE_KEYS } from "../lib/constants";

export interface TokenPair {
  access: string;
  refresh: string;
}

type Listener = () => void;

let accessToken: string | null = readStored(LOCALSTORAGE_KEYS.authAccessToken);
let refreshToken: string | null = readStored(LOCALSTORAGE_KEYS.authRefreshToken);
const listeners = new Set<Listener>();

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

export function getRefreshToken(): string | null {
  return refreshToken;
}

export function hasSession(): boolean {
  return accessToken !== null && refreshToken !== null;
}

/** Persist a freshly issued token pair and notify subscribers. */
export function setTokens(pair: TokenPair): void {
  accessToken = pair.access;
  refreshToken = pair.refresh;
  writeStored(LOCALSTORAGE_KEYS.authAccessToken, pair.access);
  writeStored(LOCALSTORAGE_KEYS.authRefreshToken, pair.refresh);
  notify();
}

/** Drop the session (logout or unrecoverable auth failure) and notify subscribers. */
export function clearTokens(): void {
  const had = accessToken !== null || refreshToken !== null;
  accessToken = null;
  refreshToken = null;
  writeStored(LOCALSTORAGE_KEYS.authAccessToken, null);
  writeStored(LOCALSTORAGE_KEYS.authRefreshToken, null);
  if (had) notify();
}

/** Subscribe to token changes (login / logout / refresh). Returns an unsubscribe. */
export function subscribe(listener: Listener): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
