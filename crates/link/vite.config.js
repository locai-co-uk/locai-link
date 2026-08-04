import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// Vite config for Tauri + bare Svelte. Dev server on 1422, HMR on 1423.
export default defineConfig({
  plugins: [svelte()],

  clearScreen: false,

  // Two entry points -> two windows: index.html (Preferences tray window) and
  // setup.html (first-run onboarding wizard). One bundle, one Tauri app.
  build: {
    rollupOptions: {
      input: {
        main: "index.html",
        setup: "setup.html",
      },
    },
  },

  server: {
    port: 1422,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1423,
        }
      : undefined,
    watch: {
      ignored: ["**/src-tauri/**"],
    },
  },
});
