import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// build into ../static so FastAPI serves it; dev server proxies /api to the running backend
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: { outDir: "../static", emptyOutDir: true, chunkSizeWarningLimit: 1600 },
  server: { proxy: { "/api": process.env.API || "http://127.0.0.1:8600" } },
});
