import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// Vite config for Tauri + bare Svelte. Tauri dispatches `tauri dev`
// and `tauri build`, both of which shell out to `npm run dev` /
// `npm run build` respectively. See tauri.conf.json.
export default defineConfig({
  plugins: [svelte()],

  // Prevent Vite from obscuring rust errors.
  clearScreen: false,

  server: {
    // Tauri expects a fixed port; strictPort fails fast if 1420 is taken
    // rather than picking a free port that Tauri isn't watching.
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // Tauri handles src-tauri/ rebuilds itself; don't trigger Vite HMR on Rust edits.
      ignored: ["**/src-tauri/**"],
    },
  },
});
