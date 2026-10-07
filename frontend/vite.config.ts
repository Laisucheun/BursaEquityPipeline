import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Backend: `.venv\Scripts\python -m bursa.cli serve` on 127.0.0.1:8000.
const API_TARGET = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    strictPort: true,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
    },
  },
  preview: {
    port: 3000,
    proxy: {
      "/api": { target: API_TARGET, changeOrigin: true },
    },
  },
});
