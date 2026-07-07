/// <reference types="svelte" />
/// <reference types="vite/client" />

// PNG imports resolve to their built URL string via Vite's asset
// handling. Declaring this here so svelte-check's TypeScript pass
// stops complaining about `import logo from "./foo.png"`.
declare module "*.png" {
  const url: string;
  export default url;
}
