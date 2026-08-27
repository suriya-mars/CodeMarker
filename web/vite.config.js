import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The API base is read at runtime from window.__API__ (set in index.html) so the
// same build works in dev and in the nginx container. In dev we proxy /api → 8000.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
    },
  },
});
