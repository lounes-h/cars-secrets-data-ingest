import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import type { Severity } from "../lib/api";
import { Icon } from "./ui";

interface Toast {
  id: number;
  severity: Severity;
  text: string;
}

const ToastContext = createContext<(severity: Severity, text: string) => void>(() => {});

export function useToast() {
  return useContext(ToastContext);
}

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const dismiss = (id: number) => setToasts((t) => t.filter((x) => x.id !== id));
  const push = useCallback((severity: Severity, text: string) => {
    const id = nextId++;
    setToasts((t) => [...t.slice(-3), { id, severity, text }]);
    // Critical alerts stay until dismissed; the rest fade on their own.
    if (severity !== "critical") setTimeout(() => dismiss(id), 8000);
  }, []);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast ${t.severity}`}>
            <span className={`sev-${t.severity}`}><Icon tone={t.severity} /></span>
            <span className="wrap-anywhere">{t.text}</span>
            <button aria-label="Dismiss" onClick={() => dismiss(t.id)}>×</button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
