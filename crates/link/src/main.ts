// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

import { mount } from "svelte";
import App from "./App.svelte";
import "./lib/tokens/tokens.css";

const target = document.getElementById("app");
if (!target) {
  throw new Error("#app root not found in index.html");
}

const app = mount(App, { target });

export default app;
