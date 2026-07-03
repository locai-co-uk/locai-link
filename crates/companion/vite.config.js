import { defineConfig } from "vite";
import { svelte } from "@sveltejs/vite-plugin-svelte";

// @ts-expect-error process is a nodejs global
const host = process.env.TAURI_DEV_HOST;

// Vite config for Tauri + bare Svelte. Ports chosen so both dev
// servers can run at once without collisions:
//   setup_assistant: server 1420, HMR 1421
//   companion:       server 1422, HMR 1423
// (Previously companion used 1421, which collided with
// setup_assistant's HMR port when both dev commands ran together.)
export default defineConfig({
  plugins: [svelte()],

  clearScreen: false,

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
