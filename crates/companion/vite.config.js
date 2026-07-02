import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// Vite config for Tauri + bare Svelte. Runs on 1421 (setup_assistant
// uses 1420) so both dev servers can be up at the same time. HMR is
// on 1423 to avoid clashing with setup_assistant's HMR on 1421.
export default defineConfig({
  plugins: [svelte()],

  clearScreen: false,

  server: {
    port: 1421,
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
