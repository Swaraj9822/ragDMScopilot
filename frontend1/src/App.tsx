import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Shell } from "./layout/Shell";

const Copilot = lazy(() => import("./pages/Copilot"));
const Observability = lazy(() => import("./pages/Observability"));
const Documents = lazy(() => import("./pages/Documents"));

export function App() {
  return (
    <Shell>
      <Suspense fallback={<div style={{ padding: "var(--s8)" }}>Loading…</div>}>
        <Routes>
          <Route path="/" element={<Navigate to="/copilot" replace />} />
          <Route path="/copilot" element={<Copilot />} />
          <Route path="/observability" element={<Observability />} />
          <Route path="/documents" element={<Documents />} />
          <Route path="*" element={<Navigate to="/copilot" replace />} />
        </Routes>
      </Suspense>
    </Shell>
  );
}
