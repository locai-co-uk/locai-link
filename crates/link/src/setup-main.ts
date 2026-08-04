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
