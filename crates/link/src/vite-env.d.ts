// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

/// <reference types="svelte" />
/// <reference types="vite/client" />

// PNG imports resolve to their built URL via Vite; declared so svelte-check's
// TS pass accepts `import logo from "./foo.png"`.
declare module "*.png" {
  const url: string;
  export default url;
}
