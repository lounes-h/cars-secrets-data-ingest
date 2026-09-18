import { useQuery } from "@tanstack/react-query";
import { NavLink, Outlet } from "react-router-dom";
import { api } from "../lib/api";
import { useLiveEvents } from "../lib/events";
import { useToast } from "./Toasts";

export function Layout({ username, onLogout }: { username: string; onLogout: () => void }) {
  const toast = useToast();
  const connected = useLiveEvents((event) => {
    // Newly raised alerts pop up wherever the user is; repeats of an open one stay quiet.
    if (event.type === "alert" && event.new && event.severity !== "info") {
      toast(event.severity as "warning" | "critical", String(event.title));
    }
  });
  const { data } = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 30_000 });
  const openAlerts = data ? data.alerts_open.critical + data.alerts_open.warning : 0;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <svg width="18" height="18" viewBox="0 0 16 16" aria-hidden="true">
            <circle cx="8" cy="8" r="6" fill="var(--accent)" />
          </svg>
          Ingest monitor
        </div>
        <nav className="nav" aria-label="Main">
          <NavLink to="/" end>Overview</NavLink>
          <NavLink to="/runs">Runs</NavLink>
          <NavLink to="/alerts">
            Alerts {openAlerts > 0 && <span className="count-pill" aria-label={`${openAlerts} open`}>{openAlerts}</span>}
          </NavLink>
          <NavLink to="/schema">Schema</NavLink>
          <NavLink to="/lots">Lots</NavLink>
        </nav>
        <div className="sidebar-foot">
          <span className="live" title={connected ? "Receiving live updates" : "Reconnecting…"}>
            <i className="dot" style={{ background: connected ? "var(--good)" : "var(--muted)" }} />
            {connected ? "Live" : "Reconnecting…"}
          </span>
          <span>
            {username} · <a href="#" onClick={(e) => { e.preventDefault(); onLogout(); }}>Sign out</a>
          </span>
        </div>
      </aside>
      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}
