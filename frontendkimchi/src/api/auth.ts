// Auth API surface: register, login, logout, and current-user lookup.
// Token persistence is handled by the token store; these helpers only deal with
// the network round-trips and keeping the store in sync.

import { apiClient, TIMEOUT_SHORT_MS } from "./client";
import { clearTokens, getRefreshToken, setTokens } from "./tokenStore";

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
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

/** Exchange credentials for a token pair and persist it. */
export async function login(email: string, password: string): Promise<TokenResponse> {
  const tokens = await apiClient.postJson<TokenResponse>(
    "/auth/login",
    { email, password },
    { timeoutMs: TIMEOUT_SHORT_MS, skipAuth: true },
  );
  setTokens({ access: tokens.access_token, refresh: tokens.refresh_token });
  return tokens;
}

/** Revoke the refresh token server-side, then clear the local session. */
export async function logout(): Promise<void> {
  const refresh = getRefreshToken();
  if (refresh) {
    try {
      await apiClient.postJson<void>(
        "/auth/logout",
        { refresh_token: refresh },
        { timeoutMs: TIMEOUT_SHORT_MS, skipAuth: true },
      );
    } catch {
      // Best-effort: even if the server call fails, drop the local session.
    }
  }
  clearTokens();
}

/** Fetch the currently authenticated user (used to validate a stored session). */
export function fetchCurrentUser(): Promise<UserPublic> {
  return apiClient.get<UserPublic>("/auth/me", { timeoutMs: TIMEOUT_SHORT_MS });
}
