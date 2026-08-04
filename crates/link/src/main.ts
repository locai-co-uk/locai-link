import { mount } from "svelte";
import App from "./App.svelte";
import "./lib/tokens/tokens.css";

const target = document.getElementById("app");
if (!target) {
  throw new Error("#app root not found in index.html");
}

const app = mount(App, { target });

export default app;
