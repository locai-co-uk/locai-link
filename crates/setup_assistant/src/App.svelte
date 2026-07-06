<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { openUrl } from "@tauri-apps/plugin-opener";
  import { getCurrentWindow } from "@tauri-apps/api/window";
  import { onMount } from "svelte";

  // Post-install user configuration flow. The .pkg installer has
  // already placed files and registered LaunchAgents; this wizard's
  // job is Sign in → Models → Serving → Permissions → Finish.
  const STEPS = [
    { id: "sign-in", label: "Sign in" },
    { id: "models", label: "Models" },
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

  // Default install root. macOS location — the .pkg postinstall
  // (bundling/pkg/scripts/postinstall) lays the launcher, boot.json,
  // and Setup Assistant.app here.
  //
  // TODO(platform-default): swap to a Tauri-side per-OS lookup once
  // @tauri-apps/plugin-os is wired.
  const DEFAULT_INSTALL_ROOT = "/Library/Locai";

  // Bootstrap runs once on mount to verify the .pkg actually
  // installed. Failure surfaces as a full-screen error instead of
  // letting the user step through a wizard against a broken install.
  type Bootstrap =
    | { kind: "checking" }
    | { kind: "ready"; install: CheckInstallResult }
    | { kind: "error"; message: string };

  // Wire shapes mirror `DeviceCodeStart` and `SignInPollResult` in
  // src-tauri/src/lib.rs. Access tokens never cross this boundary —
  // they live in the Rust `SignInState`.
  type DeviceCodeStart = {
    user_code: string;
    verification_uri: string;
    verification_uri_complete: string;
    interval: number;
    expires_in: number;
  };
  type SignInPollResult =
    | { status: "pending" }
    | { status: "slow_down" }
    | { status: "approved"; user_id: string; email: string; username: string }
    | { status: "denied" }
    | { status: "expired" }
    | { status: "error"; message: string };

  type SignIn =
    | { kind: "idle" }
    | { kind: "starting" }
    | { kind: "pending"; start: DeviceCodeStart; interval: number }
    | { kind: "approved"; email: string; username: string }
    | { kind: "error"; message: string };

  type RegisteredDevice = {
    device_id: string;
    api_key: string;
    config: unknown; // AgentConfig JSON — passed opaquely back to install_agent_config
  };

  type ModelSummary = {
    id: string;
    display_name: string;
    model_type: string;
    framework: string;
    file_extension: string;
    size_bytes: number;
    status: string;
  };

  type Models =
    | { kind: "idle" }
    | { kind: "loading" }
    | { kind: "ready"; models: ModelSummary[]; selected: Set<string> }
    | { kind: "error"; message: string };

  type Finish =
    | { kind: "idle" }
    | { kind: "registering" }
    | { kind: "writing"; registered: RegisteredDevice }
    | { kind: "deploying"; registered: RegisteredDevice; done: number; total: number }
    | {
        kind: "done";
        device_id: string;
        config_path: string | null;
        deployed_count: number;
      }
    | { kind: "error"; message: string; registered?: RegisteredDevice };

  let bootstrap = $state<Bootstrap>({ kind: "checking" });
  let current = $state(0);
  let signIn = $state<SignIn>({ kind: "idle" });
  let models = $state<Models>({ kind: "idle" });
  let finish = $state<Finish>({ kind: "idle" });
  let pollTimer: ReturnType<typeof setTimeout> | null = null;

  // Lazy-load the model catalog the first time the user lands on the
  // Models step (after sign-in, so we have a JWT). Re-navigating back
  // doesn't refetch — the list is stable enough for a single wizard
  // pass, and re-running the request would lose any selections.
  $effect(() => {
    if (
      STEPS[current].id === "models" &&
      signIn.kind === "approved" &&
      models.kind === "idle"
    ) {
      void loadModels();
    }
  });

  async function loadModels() {
    models = { kind: "loading" };
    try {
      const list = await invoke<ModelSummary[]>("list_models");
      models = { kind: "ready", models: list, selected: new Set() };
    } catch (e) {
      models = { kind: "error", message: e instanceof Error ? e.message : String(e) };
    }
  }

  function toggleModel(id: string) {
    if (models.kind !== "ready") return;
    const next = new Set(models.selected);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    models = { ...models, selected: next };
  }

  function formatBytes(n: number): string {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
    if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
    return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  }

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
    return () => {
      if (pollTimer !== null) clearTimeout(pollTimer);
    };
  });

  async function startSignIn() {
    signIn = { kind: "starting" };
    try {
      const start = await invoke<DeviceCodeStart>("sign_in_start");
      signIn = { kind: "pending", start, interval: start.interval };
      // Best-effort browser open. If it fails the user can still click
      // "Open browser again" — the verification URL is always visible.
      try {
        await openUrl(start.verification_uri_complete);
      } catch {
        // ignored; button in the UI is the fallback
      }
      schedulePoll(start.interval);
    } catch (e) {
      signIn = { kind: "error", message: e instanceof Error ? e.message : String(e) };
    }
  }

  function schedulePoll(intervalSeconds: number) {
    pollTimer = setTimeout(() => {
      void runPoll();
    }, intervalSeconds * 1000);
  }

  async function runPoll() {
    if (signIn.kind !== "pending") return;
    try {
      const result = await invoke<SignInPollResult>("sign_in_poll");
      switch (result.status) {
        case "pending":
          schedulePoll(signIn.interval);
          return;
        case "slow_down":
          // RFC 8628 §3.5 — bump interval by 5s and keep polling.
          signIn = { ...signIn, interval: signIn.interval + 5 };
          schedulePoll(signIn.interval);
          return;
        case "approved":
          signIn = { kind: "approved", email: result.email, username: result.username };
          return;
        case "denied":
          signIn = { kind: "error", message: "Sign-in was denied. You can try again." };
          return;
        case "expired":
          signIn = {
            kind: "error",
            message: "The sign-in request expired. Please start again.",
          };
          return;
        case "error":
          signIn = { kind: "error", message: result.message };
          return;
      }
    } catch (e) {
      signIn = { kind: "error", message: e instanceof Error ? e.message : String(e) };
    }
  }

  async function completeSetup() {
    // Idempotency guard for the retry path. `register_device` is not
    // safe to call twice — each call creates a new device on Control,
    // and re-clicking Try Again after a config-write failure would
    // push the account over its device cap. If we already have a
    // registered device from a prior attempt, resume from the
    // install_agent_config phase instead of starting over.
    const already: RegisteredDevice | undefined =
      finish.kind === "error" ? finish.registered : undefined;

    let registered: RegisteredDevice;
    if (already) {
      registered = already;
    } else {
      finish = { kind: "registering" };
      let deviceName: string;
      try {
        deviceName = await invoke<string>("suggest_device_name");
      } catch (e) {
        finish = { kind: "error", message: e instanceof Error ? e.message : String(e) };
        return;
      }
      try {
        registered = await invoke<RegisteredDevice>("register_device", {
          deviceName,
        });
      } catch (e) {
        finish = { kind: "error", message: e instanceof Error ? e.message : String(e) };
        return;
      }
    }

    finish = { kind: "writing", registered };
    let configPath: string | null = null;
    try {
      configPath = await invoke<string>("install_agent_config", {
        installRoot: DEFAULT_INSTALL_ROOT,
        config: registered.config,
      });
      // Register + kickstart both LaunchAgents so the runtime + menubar
      // companion come up now. No-op on Linux dev machines. If this
      // fails (e.g. write to ~/Library/LaunchAgents denied), we still
      // count the setup as successful — user can launch "Loc.ai Link"
      // from Applications later and the companion's own kickstart
      // logic will bring the runtime up.
      try {
        await invoke("install_launchagents", { installRoot: DEFAULT_INSTALL_ROOT });
      } catch (e) {
        console.warn("install_launchagents failed:", e);
      }
    } catch (e) {
      // Register succeeded on Control but we couldn't lay the config
      // down locally — flag it distinctly so the user knows the device
      // exists on the server side either way.
      finish = {
        kind: "error",
        message: e instanceof Error ? e.message : String(e),
        registered,
      };
      return;
    }

    // Queue deploys for any models the user selected on step 2.
    // Failures don't roll back the earlier steps — the device is
    // registered, the config is written, and the user can retry a
    // failed deploy from the Control UI.
    const selected =
      models.kind === "ready" ? Array.from(models.selected) : [];
    let deployedCount = 0;
    finish = {
      kind: "deploying",
      registered,
      done: 0,
      total: selected.length,
    };
    for (const modelId of selected) {
      try {
        await invoke<string>("deploy_model", {
          deviceId: registered.device_id,
          modelId,
        });
        deployedCount += 1;
        finish = {
          kind: "deploying",
          registered,
          done: deployedCount,
          total: selected.length,
        };
      } catch (e) {
        // Surface the first deploy failure but keep the device in a
        // finished-with-partial-deploys state — no reason to roll back
        // the successful deploys.
        finish = {
          kind: "error",
          message: `Deploy failed for one or more models: ${
            e instanceof Error ? e.message : String(e)
          }`,
          registered,
        };
        return;
      }
    }

    finish = {
      kind: "done",
      device_id: registered.device_id,
      config_path: configPath,
      deployed_count: deployedCount,
    };
  }

  async function reopenBrowser() {
    if (signIn.kind !== "pending") return;
    try {
      await openUrl(signIn.start.verification_uri_complete);
    } catch {
      // no-op; the URL text is on screen for manual copy
    }
  }

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

  // The Sign-in step is the only one that gates Continue on external
  // state. All later steps are placeholders that let Continue through.
  const canContinue = $derived.by(() => {
    if (STEPS[current].id === "sign-in") return signIn.kind === "approved";
    return current < STEPS.length - 1;
  });
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
            We'll open your browser to sign in. Approve this device
            there, and this window will pick up automatically.
          </p>

          {#if signIn.kind === "idle"}
            <button class="btn btn--primary btn--wide" onclick={startSignIn}>
              Sign in with Loc<span class="brand__accent">ai</span>
            </button>
          {:else if signIn.kind === "starting"}
            <div class="signin-block">
              <div class="spinner"></div>
              <div class="signin-block__msg">Requesting a sign-in code…</div>
            </div>
          {:else if signIn.kind === "pending"}
            <div class="signin-block">
              <p class="field__label">YOUR CODE</p>
              <div class="user-code">{signIn.start.user_code}</div>
              <p class="fine-print">
                If the browser didn't open, visit
                <span class="mono">{signIn.start.verification_uri}</span>
                and enter the code above.
              </p>
              <div class="row">
                <button class="btn btn--ghost" onclick={reopenBrowser}>
                  Open browser again
                </button>
                <div class="waiting">
                  <div class="spinner spinner--sm"></div>
                  <span>Waiting for approval…</span>
                </div>
              </div>
            </div>
          {:else if signIn.kind === "approved"}
            <div class="signin-block">
              <p class="field__label">SIGNED IN</p>
              <div class="approved">
                <span class="checkmark">✓</span>
                <div>
                  <div class="approved__name">{signIn.username}</div>
                  <div class="approved__email">{signIn.email}</div>
                </div>
              </div>
            </div>
          {:else if signIn.kind === "error"}
            <div class="signin-block">
              <div class="signin-block__msg signin-block__msg--error">
                {signIn.message}
              </div>
              <button class="btn btn--primary" onclick={startSignIn}>Try again</button>
            </div>
          {/if}
        {:else if STEPS[current].id === "models"}
          <p class="eyebrow">STEP 2 · MODELS</p>
          <h1>Choose models to prefetch</h1>
          <p class="lead">
            Pick which models to send to this device. You can change the
            selection later from Control.
          </p>

          {#if models.kind === "loading"}
            <div class="signin-block">
              <div class="row">
                <div class="spinner"></div>
                <span>Loading your models…</span>
              </div>
            </div>
          {:else if models.kind === "error"}
            <div class="signin-block">
              <div class="signin-block__msg signin-block__msg--error">
                {models.message}
              </div>
              <button class="btn btn--ghost" onclick={loadModels}>Retry</button>
              <p class="fine-print">
                You can continue without a selection — models can be
                deployed from Control at any time.
              </p>
            </div>
          {:else if models.kind === "ready"}
            {#if models.models.length === 0}
              <div class="signin-block">
                <div class="signin-block__msg">
                  You don't have any models on your account yet. Add
                  models from Control, then come back — or continue and
                  set them up later.
                </div>
              </div>
            {:else}
              <ul class="model-list">
                {#each models.models as m (m.id)}
                  <li class="model-row">
                    <label class="model-row__label">
                      <input
                        type="checkbox"
                        checked={models.selected.has(m.id)}
                        onchange={() => toggleModel(m.id)}
                      />
                      <div class="model-row__body">
                        <div class="model-row__name">{m.display_name}</div>
                        <div class="model-row__meta">
                          <span>{m.model_type}</span>
                          <span>·</span>
                          <span>{m.framework}</span>
                          <span>·</span>
                          <span>{formatBytes(m.size_bytes)}</span>
                        </div>
                      </div>
                    </label>
                  </li>
                {/each}
              </ul>
              <p class="fine-print">
                {models.selected.size} of {models.models.length} selected
              </p>
            {/if}
          {/if}
        {:else if STEPS[current].id === "permissions"}
          <p class="eyebrow">STEP 3 · PERMISSIONS</p>
          <h1>Runs quietly in the background</h1>
          <p class="lead">
            After setup, Loc.ai Link runs in the background and appears
            in your menubar. It will start automatically when you log
            in.
          </p>
          <p class="fine-print">
            To turn off auto-start later, open <strong>System Settings
            → General → Login Items &amp; Extensions</strong> and toggle
            <strong>Loc.ai Link</strong> off. To stop the agent entirely,
            quit it from the menubar.
          </p>
        {:else if STEPS[current].id === "finish"}
          <p class="eyebrow">STEP 4 · FINISH</p>
          <h1>Register this device</h1>
          <p class="lead">
            Enrol this machine on your Loc.ai organisation and drop the
            initial config in place. The agent picks it up on next start.
          </p>

          {#if finish.kind === "idle"}
            <button
              class="btn btn--primary btn--wide"
              onclick={completeSetup}
            >
              Complete setup
            </button>
          {:else if finish.kind === "registering"}
            <div class="signin-block">
              <div class="row">
                <div class="spinner"></div>
                <span>Registering with Control…</span>
              </div>
            </div>
          {:else if finish.kind === "writing"}
            <div class="signin-block">
              <div class="row">
                <div class="spinner"></div>
                <span>Writing agent config…</span>
              </div>
              <p class="fine-print">
                Device ID <span class="mono">{finish.registered.device_id}</span>
              </p>
            </div>
          {:else if finish.kind === "deploying"}
            <div class="signin-block">
              <div class="row">
                <div class="spinner"></div>
                <span>
                  Queueing models on this device
                  ({finish.done} of {finish.total})…
                </span>
              </div>
            </div>
          {:else if finish.kind === "done"}
            <div class="signin-block">
              <div class="approved">
                <span class="checkmark">✓</span>
                <div>
                  <div class="approved__name">All set</div>
                  <div class="approved__email">
                    Device <span class="mono">{finish.device_id}</span>
                  </div>
                </div>
              </div>
              {#if finish.config_path}
                <p class="fine-print">
                  Config written to <span class="mono">{finish.config_path}</span>
                </p>
              {/if}
              {#if finish.deployed_count > 0}
                <p class="fine-print">
                  Queued {finish.deployed_count} model{finish.deployed_count === 1 ? "" : "s"} for
                  this device. The agent will download them on next start.
                </p>
              {/if}
              <p class="fine-print">
                Loc.ai Link is now running in your menubar. You can close
                this window.
              </p>
              <button
                class="btn btn--primary btn--wide"
                onclick={() => void getCurrentWindow().close()}
              >
                Close
              </button>
            </div>
          {:else if finish.kind === "error"}
            <div class="signin-block">
              <div class="signin-block__msg signin-block__msg--error">
                {finish.message}
              </div>
              {#if finish.registered}
                <p class="fine-print">
                  The device was registered on Control
                  (<span class="mono">{finish.registered.device_id}</span>)
                  but the local config couldn't be written. You can
                  re-run this step or hand the config off manually.
                </p>
              {/if}
              <button
                class="btn btn--primary"
                onclick={completeSetup}
              >
                Try again
              </button>
            </div>
          {/if}
        {/if}
      </div>

      <footer class="bar">
        <button class="btn btn--ghost" onclick={back} disabled={current === 0}>Go Back</button>
        <button
          class="btn btn--primary"
          onclick={next}
          disabled={current === STEPS.length - 1 || !canContinue}
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

  /* --- Sign-in surfaces --------------------------------------------------- */

  .signin-block {
    display: flex;
    flex-direction: column;
    gap: var(--space-12);
    max-width: 32rem;
  }
  .signin-block__msg {
    font-size: 13px;
    color: var(--color-text-secondary);
  }
  .signin-block__msg--error {
    color: var(--color-status-conflict);
  }

  .user-code {
    font-family: var(--font-mono), monospace;
    font-size: 28px;
    font-weight: var(--weight-semibold);
    letter-spacing: 0.24em;
    color: var(--color-text-strong);
    background: var(--color-surface-cream);
    border: 1px solid var(--color-border-strong);
    border-radius: var(--radius-md);
    padding: 14px 20px;
    text-align: center;
    user-select: all;
  }

  .row {
    display: flex;
    align-items: center;
    gap: var(--space-16);
    flex-wrap: wrap;
  }
  .waiting {
    display: flex;
    align-items: center;
    gap: var(--space-8);
    font-size: 12px;
    color: var(--color-text-muted);
  }

  .approved {
    display: flex;
    align-items: center;
    gap: var(--space-12);
    padding: var(--space-12) var(--space-14);
    background: var(--color-surface-tint-green-2);
    border: 1px solid var(--color-border-green-tint);
    border-radius: var(--radius-md);
  }
  .checkmark {
    width: 26px;
    height: 26px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: var(--color-primary-bright);
    color: var(--color-ink);
    border-radius: var(--radius-pill);
    font-weight: var(--weight-bold);
  }
  .approved__name {
    font-weight: var(--weight-semibold);
    color: var(--color-text-strong);
  }
  .approved__email {
    font-size: 12px;
    color: var(--color-text-muted);
  }

  .spinner {
    width: 20px;
    height: 20px;
    border: 2px solid var(--color-border-strong);
    border-top-color: var(--color-primary);
    border-radius: var(--radius-pill);
    animation: locaiSpin 0.9s linear infinite;
  }
  .spinner--sm {
    width: 14px;
    height: 14px;
    border-width: 2px;
  }

  .mono {
    font-family: var(--font-mono), monospace;
    font-size: 11px;
    color: var(--color-text-secondary);
    word-break: break-all;
  }

  .btn--wide {
    align-self: flex-start;
    padding: 10px 22px;
    font-size: 14px;
  }

  /* --- Model catalog ------------------------------------------------------ */

  .model-list {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: var(--space-6);
    max-width: 40rem;
  }
  .model-row {
    border: 1px solid var(--color-border-hairline);
    border-radius: var(--radius-md);
    background: var(--color-surface-cream-alt);
  }
  .model-row:hover {
    border-color: var(--color-border-strong);
  }
  .model-row__label {
    display: flex;
    align-items: center;
    gap: var(--space-12);
    padding: var(--space-10) var(--space-12);
    cursor: pointer;
    width: 100%;
  }
  .model-row__label input[type="checkbox"] {
    width: 16px;
    height: 16px;
    accent-color: var(--color-primary);
    cursor: pointer;
    flex-shrink: 0;
  }
  .model-row__body {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
    flex: 1;
  }
  .model-row__name {
    font-size: 13px;
    font-weight: var(--weight-semibold);
    color: var(--color-text-strong);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .model-row__meta {
    display: flex;
    gap: var(--space-6);
    font-family: var(--font-mono), monospace;
    font-size: 11px;
    color: var(--color-text-muted);
  }

  /* --- Sign-in form (legacy) --------------------------------------------- */

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
