import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { AppShell } from "../components/common/AppShell";
import { PageLoading } from "../components/common/PageLoading";

const CopilotPage = lazy(() => import("../pages/CopilotPage"));
const ObservabilityPage = lazy(() => import("../pages/ObservabilityPage"));
const DocumentsPage = lazy(() => import("../pages/DocumentsPage"));

export function App() {
  return (
    <AppShell>
      <Suspense fallback={<PageLoading />}>
        <Routes>
          <Route path="/" element={<Navigate to="/copilot" replace />} />
          <Route path="/copilot" element={<CopilotPage />} />
          <Route path="/observability" element={<ObservabilityPage />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="*" element={<Navigate to="/copilot" replace />} />
        </Routes>
      </Suspense>
    </AppShell>
  );
}
