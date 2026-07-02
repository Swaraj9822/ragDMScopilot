import type { ReactElement, ReactNode } from "react";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "../hooks/useTheme";
import { ToastProvider } from "../hooks/useToast";
import { AuthProvider } from "../hooks/useAuth";
import { setAccessToken } from "../api/tokenStore";

export function makeTestQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
}

interface Options {
  route?: string;
  client?: QueryClient;
}

export function renderWithProviders(ui: ReactElement, options: Options = {}) {
  const client = options.client ?? makeTestQueryClient();
  // Seed an authenticated session so the app shell renders. Auth is not what
  // these tests exercise; the /auth/me handler in test/server.ts resolves the
  // user. Cleared between tests by test/setup.ts.
  setAccessToken("test-access-token");
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ThemeProvider>
        <ToastProvider>
          <AuthProvider>
            <MemoryRouter initialEntries={[options.route ?? "/"]}>{children}</MemoryRouter>
          </AuthProvider>
        </ToastProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
  return { client, ...render(ui, { wrapper }) };
}
