// SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
// SPDX-License-Identifier: BUSL-1.1

import { mount } from "svelte";
import SetupApp from "./SetupApp.svelte";
import "./lib/tokens/tokens.css";
import "./setup.css";

const target = document.getElementById("app");
if (!target) {
  throw new Error("#app root not found in setup.html");
}

const app = mount(SetupApp, { target });

export default app;
