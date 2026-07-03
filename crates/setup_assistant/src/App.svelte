<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { onMount } from "svelte";

  // Post-install user configuration flow. The .pkg installer has
  // already placed files and registered LaunchAgents; this wizard's
  // job is Sign in → Models → Serving → Permissions → Finish.
  const STEPS = [
    { id: "sign-in", label: "Sign in" },
    { id: "models", label: "Models" },
    { id: "serving", label: "Serving" },
    { id: "permissions", label: "Permissions" },
    { id: "finish", label: "Finish" },
  ] as const;

  type CheckInstallResult = {
    installed: boolean;
    version: string | null;
    path: string | null;
    boot: {
      host_app: string;
      plugin_set: string[];
      channel: string;
      asset_repo: string;
      asset_url: string | null;
    } | null;
    boot_error: string | null;
    reason: string | null;
  };

  // Default install root. macOS location — the .pkg lands here.
  // TODO(platform-default): swap to a Tauri-side per-OS lookup once
  // @tauri-apps/plugin-os is wired.
  const DEFAULT_INSTALL_ROOT = "/Library/Application Support/uk.co.locai.link";

  // Bootstrap runs once on mount to verify the .pkg actually
  // installed. Failure surfaces as a full-screen error instead of
  // letting the user step through a wizard against a broken install.
  type Bootstrap =
    | { kind: "checking" }
    | { kind: "ready"; install: CheckInstallResult }
    | { kind: "error"; message: string };

  let bootstrap = $state<Bootstrap>({ kind: "checking" });
  let current = $state(0);
  let workEmail = $state("");

  async function runBootstrapCheck() {
    try {
      const install = await invoke<CheckInstallResult>("check_install", {
        installRoot: DEFAULT_INSTALL_ROOT,
      });
      bootstrap = { kind: "ready", install };
    } catch (e) {
      bootstrap = { kind: "error", message: e instanceof Error ? e.message : String(e) };
    }
  }

  onMount(() => {
    void runBootstrapCheck();
  });

  function next() {
    if (current < STEPS.length - 1) current += 1;
  }
  function back() {
    if (current > 0) current -= 1;
  }
  function statusOf(idx: number): "done" | "current" | "pending" {
    if (idx < current) return "done";
    if (idx === current) return "current";
    return "pending";
  }
</script>

{#if bootstrap.kind !== "ready"}
  <div class="splash">
    <div class="splash__mark">Loc<span class="brand__accent">ai</span> Link</div>
    {#if bootstrap.kind === "checking"}
      <div class="splash__msg">Checking installation…</div>
    {:else}
      <div class="splash__msg splash__msg--error">
        Setup couldn't verify the install.
      </div>
      <div class="splash__detail">{bootstrap.message}</div>
    {/if}
  </div>
{:else}
  <main class="wizard">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand__tab">SETUP ASSISTANT</div>
        <div class="brand__mark">Loc<span class="brand__accent">ai</span> Link</div>
      </div>

      <ol class="steps">
        {#each STEPS as step, i (step.id)}
          <li class="step step--{statusOf(i)}">
            <span class="step__num">{i + 1}</span>
            <span class="step__label">{step.label}</span>
          </li>
        {/each}
      </ol>

      {#if bootstrap.install.installed && bootstrap.install.version}
        <div class="rail-foot">
          v{bootstrap.install.version}
        </div>
      {/if}
    </aside>

    <section class="content">
      <div class="content__body">
        {#if STEPS[current].id === "sign-in"}
          <p class="eyebrow">STEP 1 · CONNECT</p>
          <h1>Sign in to Locai</h1>
          <p class="lead">
            Connect this node to your organisation so it can pull the
            models on your account and register with your Control Plane.
          </p>

          <label class="field">
            <span class="field__label">WORK EMAIL</span>
            <input
              class="field__input"
              type="email"
              placeholder="you@company.com"
              bind:value={workEmail}
              autocomplete="email"
            />
          </label>

          <p class="fine-print">
            No account on this device? You can also pair with a device
            code from the Control Plane. Organisation detection will
            wire up here once the backend endpoint is available.
          </p>
        {:else if STEPS[current].id === "models"}
          <p class="eyebrow">STEP 2 · MODELS</p>
          <h1>Choose models to prefetch</h1>
          <p class="lead">Placeholder — model catalog + selection UI wires in here.</p>
        {:else if STEPS[current].id === "serving"}
          <p class="eyebrow">STEP 3 · SERVING</p>
          <h1>Serving ports</h1>
          <p class="lead">Placeholder — port + host configuration wires in here.</p>
        {:else if STEPS[current].id === "permissions"}
          <p class="eyebrow">STEP 4 · PERMISSIONS</p>
          <h1>macOS permissions</h1>
          <p class="lead">Placeholder — Login Items / Notifications prompts wire in here.</p>
        {:else if STEPS[current].id === "finish"}
          <p class="eyebrow">STEP 5 · FINISH</p>
          <h1>All set</h1>
          <p class="lead">Placeholder — success state + Control Plane handoff wires in here.</p>
        {/if}
      </div>

      <footer class="bar">
        <button class="btn btn--ghost" onclick={back} disabled={current === 0}>Go Back</button>
        <button
          class="btn btn--primary"
          onclick={next}
          disabled={current === STEPS.length - 1}
        >
          Continue
        </button>
      </footer>
    </section>
  </main>
{/if}

<style>
  /* Global reset (border-box, body margin, hidden scrollbars) lives in
     lib/tokens/tokens.css so it applies without Svelte scoping quirks. */

  .wizard {
    display: flex;
    flex-direction: row;
    width: 100vw;
    height: 100vh;
    overflow: hidden;
    background: var(--color-paper);
    color: var(--color-text);
    font-family: var(--font-body), var(--font-system);
  }

  /* --- Bootstrap splash ---------------------------------------------------- */

  .splash {
    height: 100vh;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: var(--space-16);
    background: var(--color-surface-dark-rail);
    color: var(--color-text-on-dark);
    font-family: var(--font-body), var(--font-system);
    padding: var(--space-34);
    text-align: center;
  }
  .splash__mark {
    font-family: var(--font-display), var(--font-system);
    font-weight: var(--weight-extrabold);
    font-size: 28px;
    letter-spacing: var(--tracking-display);
    line-height: 1;
  }
  .splash__msg {
    font-size: 14px;
    color: var(--color-text-on-dark-70);
  }
  .splash__msg--error {
    color: var(--color-status-conflict);
  }
  .splash__detail {
    font-family: var(--font-mono), monospace;
    font-size: 12px;
    color: var(--color-text-on-dark-45);
    max-width: 60ch;
    word-break: break-word;
  }

  /* --- Sidebar (dark rail) ------------------------------------------------- */

  .sidebar {
    width: 200px;
    flex-shrink: 0;
    padding: var(--space-26) var(--space-22) var(--space-22);
    background: var(--color-surface-dark-rail);
    color: var(--color-text-on-dark);
    display: flex;
    flex-direction: column;
    gap: var(--space-30);
  }

  .brand__tab {
    font-family: var(--font-mono), monospace;
    font-size: 10px;
    letter-spacing: var(--tracking-mono-md);
    color: var(--color-text-on-dark-45);
    text-transform: uppercase;
    margin-bottom: var(--space-8);
  }

  .brand__mark {
    font-family: var(--font-display), var(--font-system);
    font-weight: var(--weight-extrabold);
    font-size: 22px;
    letter-spacing: var(--tracking-display);
    line-height: 1;
    color: var(--color-text-on-dark);
  }

  .brand__accent {
    background: var(--color-primary-bright);
    color: var(--color-ink);
    padding: 0 4px;
    border-radius: var(--radius-xs);
    margin: 0 1px;
  }

  .steps {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-14);
  }

  .step {
    display: flex;
    align-items: center;
    gap: var(--space-12);
    font-size: 14px;
    font-weight: var(--weight-medium);
    color: var(--color-text-on-dark-55);
  }

  .step__num {
    width: 22px;
    height: 22px;
    border-radius: var(--radius-pill);
    border: 1.5px solid var(--color-text-on-dark-45);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: var(--weight-semibold);
    color: var(--color-text-on-dark-55);
    flex-shrink: 0;
  }

  .step--current {
    color: var(--color-text-on-dark);
  }
  .step--current .step__num,
  .step--done .step__num {
    background: var(--color-primary-bright);
    border-color: var(--color-primary-bright);
    color: var(--color-ink);
  }
  .step--done {
    color: var(--color-text-on-dark-55);
  }

  .rail-foot {
    margin-top: auto;
    font-family: var(--font-mono), monospace;
    font-size: 10px;
    letter-spacing: var(--tracking-mono-md);
    color: var(--color-text-on-dark-45);
  }

  /* --- Content pane -------------------------------------------------------- */

  .content {
    flex: 1;
    display: flex;
    flex-direction: column;
    padding: var(--space-34);
    gap: var(--space-22);
    min-width: 0;
  }

  .content__body {
    flex: 1;
    display: flex;
    flex-direction: column;
    gap: var(--space-16);
    overflow-y: auto;
  }

  .eyebrow {
    font-family: var(--font-mono), monospace;
    font-size: 11px;
    font-weight: var(--weight-semibold);
    letter-spacing: var(--tracking-mono-md);
    color: var(--color-primary);
    margin: 0;
  }

  h1 {
    font-family: var(--font-display), var(--font-system);
    font-weight: var(--weight-extrabold);
    font-size: 26px;
    letter-spacing: var(--tracking-display);
    color: var(--color-text-strong);
    margin: 0;
  }

  .lead {
    font-size: 14px;
    line-height: 1.55;
    color: var(--color-text-secondary);
    margin: 0;
    max-width: 46ch;
  }

  .fine-print {
    font-size: 12px;
    line-height: 1.55;
    color: var(--color-text-muted);
    margin: var(--space-8) 0 0 0;
    max-width: 46ch;
  }

  /* --- Sign-in form ------------------------------------------------------- */

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
    max-width: 32rem;
  }

  .field__label {
    font-family: var(--font-mono), monospace;
    font-size: 10px;
    letter-spacing: var(--tracking-mono-md);
    color: var(--color-text-muted);
    text-transform: uppercase;
  }

  .field__input {
    font-family: var(--font-body), var(--font-system);
    font-size: 14px;
    padding: 10px 14px;
    border-radius: var(--radius-input);
    border: 1px solid var(--color-border-strong);
    background: var(--color-surface);
    color: var(--color-text-strong);
  }
  .field__input:focus {
    outline: none;
    border-color: var(--color-primary-bright);
    box-shadow: 0 0 0 3px rgba(0, 210, 106, 0.15);
  }

  /* --- Footer bar --------------------------------------------------------- */

  .bar {
    display: flex;
    justify-content: flex-end;
    gap: var(--space-10);
    border-top: 1px solid var(--color-border-hairline);
    padding-top: var(--space-16);
  }

  .btn {
    font-family: var(--font-body), var(--font-system);
    font-size: 13px;
    font-weight: var(--weight-semibold);
    padding: 8px 18px;
    border-radius: var(--radius-control);
    cursor: pointer;
    transition:
      background var(--motion-hover) var(--easing-out),
      border-color var(--motion-hover) var(--easing-out);
  }

  .btn--primary {
    background: var(--color-primary);
    color: var(--color-text-on-dark);
    border: 1px solid var(--color-primary-pressed);
    box-shadow: var(--shadow-button-sm);
  }
  .btn--primary:hover:not(:disabled) {
    background: var(--color-primary-bright);
  }
  .btn--primary:disabled {
    background: var(--color-text-disabled);
    border-color: var(--color-text-disabled);
    cursor: not-allowed;
  }

  .btn--ghost {
    background: var(--color-paper);
    color: var(--color-text-strong);
    border: 1px solid var(--color-border-input);
  }
  .btn--ghost:hover:not(:disabled) {
    background: var(--color-surface-hover);
  }
  .btn--ghost:disabled {
    color: var(--color-text-disabled);
    border-color: var(--color-border-hairline);
    cursor: not-allowed;
  }
</style>
