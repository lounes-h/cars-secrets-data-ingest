import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ApiError } from "./lib/api";
import "./styles.css";

// An expired session anywhere sends the user back to the login screen.
function onError(err: unknown) {
  if (err instanceof ApiError && err.status === 401) {
    queryClient.setQueryData(["me"], null);
  }
}

const queryClient: QueryClient = new QueryClient({
  queryCache: new QueryCache({ onError }),
  mutationCache: new MutationCache({ onError }),
  defaultOptions: {
    queries: { staleTime: 10_000, refetchOnWindowFocus: true, placeholderData: (prev: unknown) => prev },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
