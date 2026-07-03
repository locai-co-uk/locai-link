<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";

  // Post-install wizard. Step list intentionally excludes Licence /
  // Destination — those live in the .pkg installer's native chrome
  // that runs before this app.
  const STEPS = [
    { id: "welcome", label: "Introduction" },
    { id: "check-install", label: "Check Install" },
    { id: "components", label: "Components" },
    { id: "network", label: "Network & Permissions" },
    { id: "install", label: "Installation" },
    { id: "summary", label: "Summary" },
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
    reason: string | null;
  };

  // Default install root. Set to the macOS location since that's the
  // initial .pkg target; the field is editable, so Windows / Linux
  // testers can override without needing a per-platform default here.
  // TODO(platform-default): move to a Tauri command that returns the
  // per-OS default once the plugin-os dep is added.
  const DEFAULT_INSTALL_ROOT = "/Library/Application Support/uk.co.locai.link";

  let current = $state(0);
  let installRoot = $state(DEFAULT_INSTALL_ROOT);
  let checkResult = $state<CheckInstallResult | null>(null);
  let checkError = $state<string | null>(null);
  let checking = $state(false);

  // Kick the install-root probe the first time we land on step 2.
  // Cheap enough to redo on every re-entry, so we don't bother
  // caching — the user may correct the path field between visits.
  async function runCheck() {
    checking = true;
    checkError = null;
    checkResult = null;
    try {
      checkResult = await invoke<CheckInstallResult>("check_install", {
        installRoot,
      });
    } catch (e) {
      checkError = e instanceof Error ? e.message : String(e);
    } finally {
      checking = false;
    }
  }

  function next() {
    if (current < STEPS.length - 1) {
      current += 1;
      if (STEPS[current].id === "check-install") {
        void runCheck();
      }
    }
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
  </aside>

  <section class="content">
    <div class="content__body">
      {#if current === 0}
        <p class="eyebrow">LOC.AI LINK · SETUP</p>
        <h1>Welcome to Loc.ai Link Setup</h1>
        <p class="lead">
          Loc.ai Link is installed. This short setup gets the on-device agent
          configured and connected to your Control Plane.
        </p>
        <ul class="features">
          <li>Check the existing installation</li>
          <li>Choose components and models</li>
          <li>Register the agent to run at login</li>
        </ul>
        <p class="hint">Click Continue to begin.</p>
      {:else if current === 1}
        <p class="eyebrow">STEP 2 · CHECK INSTALL</p>
        <h1>Existing installation</h1>
        <p class="lead">
          Reading <code>boot.json</code> and the <code>current</code> version
          pointer from the install root.
        </p>

        <label class="field">
          <span class="field__label">Install root</span>
          <input class="field__input" type="text" bind:value={installRoot} />
        </label>

        <div class="row">
          <button class="btn btn--ghost" onclick={runCheck} disabled={checking}>
            {checking ? "Checking…" : "Re-check"}
          </button>
        </div>

        {#if checkError}
          <div class="callout callout--error">
            <strong>Check failed:</strong> {checkError}
          </div>
        {:else if checkResult}
          {#if checkResult.installed}
            <div class="callout callout--ok">
              <strong>Found Loc.ai Link {checkResult.version}</strong>
              <p class="callout__body">{checkResult.path}</p>
            </div>
            {#if checkResult.boot}
              <dl class="kv">
                <div><dt>Channel</dt><dd>{checkResult.boot.channel}</dd></div>
                <div><dt>Asset repo</dt><dd>{checkResult.boot.asset_repo}</dd></div>
                <div>
                  <dt>Plugins</dt>
                  <dd>
                    {checkResult.boot.plugin_set.length
                      ? checkResult.boot.plugin_set.join(", ")
                      : "(none)"}
                  </dd>
                </div>
              </dl>
            {/if}
          {:else}
            <div class="callout callout--warn">
              <strong>No existing install found</strong>
              <p class="callout__body">{checkResult.reason}</p>
            </div>
          {/if}
        {/if}
      {:else}
        <p class="eyebrow">STEP {current + 1} · {STEPS[current].label.toUpperCase()}</p>
        <h1>{STEPS[current].label}</h1>
        <p class="lead">Placeholder — this step is not built yet.</p>
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

<style>
  /* border-box everywhere — with default content-box, every padding
     value adds to declared width/height and small px overflows sneak
     in even when the math looks like it should fit. */
  :global(*, *::before, *::after) {
    box-sizing: border-box;
  }
  :global(html, body) {
    margin: 0;
    padding: 0;
    height: 100vh;
    overflow: hidden; /* wizard is a fixed-size window; nothing outside it should scroll */
  }
  :global(#app) {
    height: 100vh;
  }

  .wizard {
    display: flex;
    height: 100vh;
    /* Belt-and-suspenders: even if the :global() reset misses body's
       default 8px margin, clipping here guarantees no outer scroll. */
    overflow: hidden;
    background: var(--color-paper);
    color: var(--color-text);
    font-family: var(--font-body), var(--font-system);
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

  .features {
    list-style: none;
    padding: 0;
    margin: var(--space-6) 0 0 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-8);
  }

  .features li {
    position: relative;
    padding-left: 20px;
    font-size: 13px;
    color: var(--color-text-secondary);
  }

  .features li::before {
    content: "";
    position: absolute;
    left: 0;
    top: 7px;
    width: 7px;
    height: 7px;
    border-radius: var(--radius-pill);
    background: var(--color-primary-bright);
  }

  .hint {
    font-size: 12px;
    color: var(--color-text-muted);
    margin: var(--space-8) 0 0 0;
  }

  code {
    font-family: var(--font-mono), monospace;
    font-size: 12px;
    background: var(--color-surface-cream);
    padding: 1px 5px;
    border-radius: var(--radius-xs);
    color: var(--color-text-strong);
  }

  /* --- Check Install form + result ---------------------------------------- */

  .field {
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
  }

  .field__label {
    font-size: 12px;
    font-weight: var(--weight-medium);
    color: var(--color-text-muted);
  }

  .field__input {
    font-family: var(--font-mono), monospace;
    font-size: 12px;
    padding: 8px 10px;
    border-radius: var(--radius-input);
    border: 1px solid var(--color-border-strong);
    background: var(--color-surface-cream);
    color: var(--color-text-strong);
  }
  .field__input:focus {
    outline: none;
    border-color: var(--color-primary-bright);
  }

  .row {
    display: flex;
    gap: var(--space-10);
    align-items: center;
  }

  .callout {
    padding: var(--space-12) var(--space-16);
    border-radius: var(--radius-md);
    border: 1px solid var(--color-border);
    background: var(--color-surface-cream);
  }
  .callout strong {
    display: block;
    font-weight: var(--weight-semibold);
    color: var(--color-text-strong);
    font-size: 13px;
  }
  .callout__body {
    margin: 4px 0 0 0;
    font-size: 12px;
    color: var(--color-text-secondary);
    font-family: var(--font-mono), monospace;
    word-break: break-all;
  }

  .callout--ok {
    background: var(--color-surface-tint-green-2);
    border-color: var(--color-border-green-tint);
  }
  .callout--warn {
    background: #FFF8EB;
    border-color: #F0D999;
  }
  .callout--error {
    background: #FDECE9;
    border-color: #F0B4AC;
  }

  .kv {
    display: grid;
    grid-template-columns: max-content 1fr;
    row-gap: 4px;
    column-gap: var(--space-16);
    margin: 0;
    font-size: 12px;
  }
  .kv > div { display: contents; }
  .kv dt {
    color: var(--color-text-muted);
    font-weight: var(--weight-medium);
  }
  .kv dd {
    margin: 0;
    color: var(--color-text-strong);
    font-family: var(--font-mono), monospace;
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
