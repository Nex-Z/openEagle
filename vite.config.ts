import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: "127.0.0.1",
    port: 1420,
    strictPort: true
  },
  build: {
    rollupOptions: {
      input: ["index.html", "solo-overlay.html"]
    }
  },
  clearScreen: false
});
