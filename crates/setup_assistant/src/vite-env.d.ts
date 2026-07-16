/// <reference types="svelte" />
/// <reference types="vite/client" />

// PNG imports resolve to their built URL via Vite; declared so svelte-check's
// TS pass accepts `import logo from "./foo.png"`.
declare module "*.png" {
  const url: string;
  export default url;
}
