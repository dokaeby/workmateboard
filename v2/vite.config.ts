import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // FastAPI가 /app/ 아래 정적 서빙할 때도 동작하도록 상대 경로
  base: "./",
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8017",
      "/static": "http://127.0.0.1:8017",
      "/share": "http://127.0.0.1:8017",
    },
  },
  build: { chunkSizeWarningLimit: 900 },
});
