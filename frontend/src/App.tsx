import { useQuery, useQueryClient } from "@tanstack/react-query";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { ToastProvider } from "./components/Toasts";
import { api, ApiError } from "./lib/api";
import { AlertsPage } from "./pages/Alerts";
import { LoginPage } from "./pages/Login";
import { LotDetailPage, LotsPage } from "./pages/Lots";
import { OverviewPage } from "./pages/Overview";
import { RunDetailPage, RunsPage } from "./pages/Runs";
import { SchemaPage } from "./pages/Schema";

export function App() {
  const qc = useQueryClient();
  const me = useQuery({
    queryKey: ["me"],
    queryFn: api.me,
    retry: (count, err) => !(err instanceof ApiError && err.status === 401) && count < 2,
  });

  if (me.isLoading) return <div className="empty">Loading…</div>;
  if (!me.data) {
    return <LoginPage onLoggedIn={() => qc.invalidateQueries()} />;
  }

  const logout = async () => {
    await api.logout();
    // Reset, not clear: clearing detached the mounted "me" query, so the dashboard stayed up.
    qc.resetQueries();
  };

  return (
    <ToastProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<Layout username={me.data.username} onLogout={logout} />}>
            <Route index element={<OverviewPage />} />
            <Route path="runs" element={<RunsPage />} />
            <Route path="runs/:id" element={<RunDetailPage />} />
            <Route path="alerts" element={<AlertsPage />} />
            <Route path="schema" element={<SchemaPage />} />
            <Route path="lots" element={<LotsPage />} />
            <Route path="lots/:source/:lotNumber" element={<LotDetailPage />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
      </BrowserRouter>
    </ToastProvider>
  );
}
