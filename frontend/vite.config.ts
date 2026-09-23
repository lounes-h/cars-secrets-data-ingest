import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // `npm run dev` against a locally running API (uvicorn on :8000)
    proxy: { "/api": { target: "http://localhost:8000", changeOrigin: false } },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test-setup.ts"],
    // `npm run test:coverage` fails below 95 % on any metric (see CLAUDE.md, Tests).
    coverage: {
      provider: "v8",
      include: ["src/**/*.{ts,tsx}"],
      thresholds: { statements: 95, branches: 95, functions: 95, lines: 95 },
    },
  },
});
