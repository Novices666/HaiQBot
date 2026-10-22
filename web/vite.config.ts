import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 构建产物直接落到 Python 包里（dist 入库，随 wheel 分发）。
// 开发时 `npm run dev` 用 5173，代理到后端 8080。
export default defineConfig({
  plugins: [react()],
  base: "/",
  build: {
    outDir: "../src/haiqbot/webui/dist",
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8080", changeOrigin: false },
      "/i": { target: "http://127.0.0.1:8080", changeOrigin: false },
    },
  },
});
