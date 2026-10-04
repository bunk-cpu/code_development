import { resolve } from "node:path"
import vue from "@vitejs/plugin-vue"
import { defineConfig } from "vite"

export default defineConfig(({ command }) => ({
  plugins: [vue()],
  base: command === "build" ? "/internal-static/" : "/",
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/session": "http://127.0.0.1:8000",
      "^/internal(?:/|$)": "http://127.0.0.1:8000",
      "/docs": "http://127.0.0.1:8000",
      "/openapi.json": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist/internal",
    emptyOutDir: true,
    rolldownOptions: { input: resolve(import.meta.dirname, "internal.html") },
  },
}))
