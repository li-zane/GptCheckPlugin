import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "VITE_BACKEND_TARGET");
  return {
    plugins: [react()],
    server: {
      port: 5176,
      strictPort: true,
      proxy: {
        "^/api(?:/|$)": {
          target: env.VITE_BACKEND_TARGET || "http://127.0.0.1:8000",
          changeOrigin: true,
        },
      },
    },
    build: {
      rollupOptions: {
        output: {
          manualChunks(id) {
            const normalizedId = id.replace(/\\/g, "/");
            if (!normalizedId.includes("/node_modules/")) return undefined;
            if (
              normalizedId.includes("/react/")
              || normalizedId.includes("/react-dom/")
              || normalizedId.includes("/scheduler/")
            ) {
              return "react-vendor";
            }
            if (normalizedId.includes("/lucide-react/")) return "icons";
            return undefined;
          },
        },
      },
    },
  };
});
