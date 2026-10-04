import { resolve } from "node:path"
import vue from "@vitejs/plugin-vue"
import { defineConfig } from "vite"

export default defineConfig(({ command }) => ({
  plugins: [vue()],
  base: command === "build" ? "/customer-static/" : "/",
  server: {
    host: "127.0.0.1",
    port: 5174,
    strictPort: true,
    proxy: {
      "/session": "http://127.0.0.1:8000",
      "^/customer(?:/|$)": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist/customer",
    emptyOutDir: true,
    rolldownOptions: { input: resolve(import.meta.dirname, "customer.html") },
  },
}))
