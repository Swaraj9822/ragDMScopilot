import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import {
  fetchCurrentUser,
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
  type UserPublic,
} from "../api/auth";
import { hasSession, subscribe } from "../api/tokenStore";
import { refreshAccessToken } from "../api/client";
import { clearUserData } from "../lib/sessionData";

type AuthStatus = "loading" | "authenticated" | "unauthenticated";

interface AuthContextValue {
  status: AuthStatus;
  user: UserPublic | null;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, password: string) => Promise<UserPublic>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  // Start in "loading": the access token is memory-only, so on a fresh page
  // load we don't yet know if a session exists — an httpOnly refresh cookie may
  // still be valid. The mount effect resolves this via a silent refresh.
  const [status, setStatus] = useState<AuthStatus>("loading");
  const [user, setUser] = useState<UserPublic | null>(null);
  const mounted = useRef(true);

  // Restore/validate the session once on mount. When there is no in-memory
  // access token (e.g. right after a reload), attempt a silent refresh first:
  // the browser still holds the httpOnly refresh cookie, so one /auth/refresh
  // round-trip re-establishes the session without persisting the access token.
  useEffect(() => {
    mounted.current = true;
    (async () => {
      if (!hasSession()) {
        const refreshed = await refreshAccessToken();
        if (!refreshed) {
          if (mounted.current) setStatus("unauthenticated");
          return;
        }
      }
      try {
        const u = await fetchCurrentUser();
        if (!mounted.current) return;
        setUser(u);
        setStatus("authenticated");
      } catch {
        if (!mounted.current) return;
        // Invalid/expired session (the client already cleared tokens on a
        // failed refresh). Fall back to the login screen.
        setUser(null);
        setStatus("unauthenticated");
      }
    })();
    return () => {
      mounted.current = false;
    };
  }, []);

  // React to tokens being cleared elsewhere (e.g. the client dropping the
  // session after a failed refresh) so the UI redirects to login immediately.
  useEffect(() => {
    return subscribe(() => {
      if (!hasSession()) {
        // Drop any cached user data so the login screen — and the next user on
        // this browser — starts fresh. Covers both manual logout and the client
        // clearing tokens after a failed refresh.
        clearUserData();
        setUser(null);
        setStatus("unauthenticated");
      }
    });
  }, []);

  const login = useCallback(async (email: string, password: string) => {
    await apiLogin(email, password);
    const u = await fetchCurrentUser();
    setUser(u);
    setStatus("authenticated");
  }, []);

  const register = useCallback(
    (email: string, password: string) => apiRegister(email, password),
    [],
  );

  const logout = useCallback(async () => {
    await apiLogout();
    setUser(null);
    setStatus("unauthenticated");
  }, []);

  const value = useMemo(
    () => ({ status, user, login, register, logout }),
    [status, user, login, register, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used within AuthProvider");
  return ctx;
}
