import { Suspense, lazy, useEffect, useRef } from "react";
import { Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { AppShell } from "../components/common/AppShell";
import { PageLoading } from "../components/common/PageLoading";
import { useAuth } from "../hooks/useAuth";
import LoginPage from "../pages/LoginPage";

// Lazy-load pages in production to defer heavy chunks until each tab is visited.
// In Vitest, eager imports avoid flakiness from unresolved dynamic imports in jsdom.
const isTest = import.meta.env.MODE === "test";

const CopilotPage = isTest
  ? (await import("../pages/CopilotPage")).default
  : lazy(() => import("../pages/CopilotPage"));
const ObservabilityPage = isTest
  ? (await import("../pages/ObservabilityPage")).default
  : lazy(() => import("../pages/ObservabilityPage"));
const DocumentsPage = isTest
  ? (await import("../pages/DocumentsPage")).default
  : lazy(() => import("../pages/DocumentsPage"));
const SqlLabPage = isTest
  ? (await import("../pages/SqlLabPage")).default
  : lazy(() => import("../pages/SqlLabPage"));
const EvaluationPage = isTest
  ? (await import("../pages/EvaluationPage")).default
  : lazy(() => import("../pages/EvaluationPage"));
const FeedbackInboxPage = isTest
  ? (await import("../pages/FeedbackInboxPage")).default
  : lazy(() => import("../pages/FeedbackInboxPage"));
const ReplayLabPage = isTest
  ? (await import("../pages/ReplayLabPage")).default
  : lazy(() => import("../pages/ReplayLabPage"));
const KnowledgeGapMapPage = isTest
  ? (await import("../pages/KnowledgeGapMapPage")).default
  : lazy(() => import("../pages/KnowledgeGapMapPage"));

export function App() {
  const { status } = useAuth();
  const navigate = useNavigate();
  const wasAuthenticated = useRef(false);

  // Whenever the user becomes authenticated — a fresh sign-in or a resumed
  // session when the app is (re)opened — start on a clean Copilot tab instead
  // of restoring whatever URL happened to be in the address bar. The ref guard
  // means this only fires on the unauthenticated→authenticated transition, so
  // in-app navigation between tabs is left untouched.
  useEffect(() => {
    if (status === "authenticated" && !wasAuthenticated.current) {
      wasAuthenticated.current = true;
      navigate("/copilot", { replace: true });
    } else if (status === "unauthenticated") {
      wasAuthenticated.current = false;
    }
  }, [status, navigate]);

  // Resolving a stored session — hold the chrome back until we know.
  if (status === "loading") {
    return (
      <div style={{ padding: "var(--space-6)" }}>
        <PageLoading />
      </div>
    );
  }

  // No valid session — the login screen owns the whole viewport (no app shell).
  if (status === "unauthenticated") {
    return <LoginPage />;
  }

  return (
    <AppShell>
      <Suspense fallback={<PageLoading />}>
        <Routes>
          <Route path="/" element={<Navigate to="/copilot" replace />} />
          <Route path="/copilot" element={<CopilotPage />} />
          <Route path="/observability" element={<ObservabilityPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/sql-lab" element={<SqlLabPage />} />
          <Route path="/evaluation" element={<EvaluationPage />} />
          <Route path="/feedback" element={<FeedbackInboxPage />} />
          <Route path="/replay" element={<ReplayLabPage />} />
          <Route path="/knowledge-gap" element={<KnowledgeGapMapPage />} />
          <Route path="*" element={<Navigate to="/copilot" replace />} />
        </Routes>
      </Suspense>
    </AppShell>
  );
}
