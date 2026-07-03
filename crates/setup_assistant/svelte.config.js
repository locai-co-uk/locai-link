import { vitePreprocess } from "@sveltejs/vite-plugin-svelte";

// Bare Svelte + Vite. svelte-check reads this file for compiler
// options; without it svelte-check errors on load.
export default {
  preprocess: vitePreprocess(),
};
