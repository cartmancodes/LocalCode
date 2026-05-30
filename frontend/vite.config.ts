import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // Single rule covers both REST and the chat WebSocket at /api/sessions/:id/ws.
      // `ws: true` is what makes vite upgrade the connection rather than HTTP-proxying it.
      "/api": {
        target: "http://127.0.0.1:8080",
        ws: true,
        changeOrigin: true,
      },
    },
  },
});
