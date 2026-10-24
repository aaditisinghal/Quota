import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, the API runs separately (`python -m quota serve`); `npm run build` writes into the Python package,
// where FastAPI serves it at /.
const api = "http://127.0.0.1:8000";
const paths = ["/state", "/timeline", "/trace", "/ledger", "/today", "/scorecard", "/messages", "/chat", "/inject"];

export default defineConfig({
  plugins: [react()],
  // An inline (empty) PostCSS config stops Vite searching parent folders for someone else's postcss.config.
  css: { postcss: {} },
  build: { outDir: "../quota/api/static", emptyOutDir: true },
  server: { proxy: Object.fromEntries(paths.map((p) => [p, api])) },
});
