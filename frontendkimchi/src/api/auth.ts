// Auth API surface: register, login, logout, and current-user lookup.
// Token persistence is handled by the token store; these helpers only deal with
// the network round-trips and keeping the store in sync.
//
// The refresh token is never handled here — it lives in an httpOnly cookie the
// browser manages. Login/logout use credentials:"include" so the browser
// stores/sends that cookie; only the access token is kept in the token store.

import { apiClient, TIMEOUT_SHORT_MS } from "./client";
import { clearTokens, setAccessToken } from "./tokenStore";

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface UserPublic {
  id: string;
  email: string;
  is_active: boolean;
  created_at: string;
}

/** Create a new account. Does not log the user in (no tokens are issued). */
export function register(email: string, password: string): Promise<UserPublic> {
  return apiClient.postJson<UserPublic>(
    "/auth/register",
    { email, password },
    { timeoutMs: TIMEOUT_SHORT_MS, skipAuth: true },
  );
}

/** Exchange credentials for an access token (+ refresh cookie) and persist it. */
export async function login(email: string, password: string): Promise<LoginResponse> {
  const tokens = await apiClient.postJson<LoginResponse>(
    "/auth/login",
    { email, password },
    { timeoutMs: TIMEOUT_SHORT_MS, skipAuth: true, credentials: "include" },
  );
  setAccessToken(tokens.access_token);
  return tokens;
}

/** Revoke the refresh token server-side (via its cookie), then clear locally. */
export async function logout(): Promise<void> {
  try {
    await apiClient.postJson<void>(
      "/auth/logout",
      {},
      { timeoutMs: TIMEOUT_SHORT_MS, skipAuth: true, credentials: "include" },
    );
  } catch {
    // Best-effort: even if the server call fails, drop the local session.
  }
  clearTokens();
}

/** Fetch the currently authenticated user (used to validate a stored session). */
export function fetchCurrentUser(): Promise<UserPublic> {
  return apiClient.get<UserPublic>("/auth/me", { timeoutMs: TIMEOUT_SHORT_MS });
}
