<script lang="ts">
  import { invoke } from "@tauri-apps/api/core";
  import { openUrl } from "@tauri-apps/plugin-opener";
  import locaiLogo from "./lib/locai-logo.png";
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
    device_id: string | null;
    device_name: string | null;
  };

  // Default install root. macOS location — the .pkg postinstall
  // (bundling/pkg/scripts/postinstall) lays the launcher, boot.json,
  // and Setup Assistant.app here.
  //
  // Loaded from Rust on mount so the value branches per OS:
  //   * macOS  → /Library/Locai
  //   * Linux  → $HOME/.local/share/locai
  // Empty string until the async fetch resolves; every call site is
  // gated so an empty root doesn't leak into an invoke() call.
  let installRoot = $state<string>("");

  // OS the SA is running on ("macos" / "linux" / other). Frontend uses
  // this to render menubar vs tray, System Settings vs systemctl, etc.
  // Loaded from Rust on mount alongside installRoot.
  let platform = $state<string>("");

  // "splash" — check_install found a registered device, so show the
  //   "already set up" chooser (Open Preferences / Re-register /
  //   Uninstall). This is the default landing state for re-runs of the
  //   SA on a machine that's already onboarded.
  // "wizard" — either a fresh install (no session file) OR the user
  //   picked Re-register on the splash and confirmed. Runs the normal
  //   sign-in → models → permissions → finish flow.
  let mode = $state<"splash" | "wizard">("wizard");

  // When set, the Finish step's completeSetup() runs `re_register`
  // (Control DELETE + local wipe) before minting the new registration
  // key. Cleared after that call succeeds. Carrying the old id here —
  // rather than re-reading check_install at Finish time — means the
  // wipe uses the id the user actually saw on the splash.
  let pendingReRegister = $state<{ deviceId: string; deviceName: string | null } | null>(null);

  // Splash action UI state — one at a time, no concurrency.
  let splashAction = $state<
    | { kind: "idle" }
    | { kind: "confirming"; action: "re-register" | "uninstall" }
    | { kind: "working"; message: string }
    | { kind: "error"; message: string }
  >({ kind: "idle" });

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
    | { kind: "minting" }
    | { kind: "registering" }
    | { kind: "writing"; registered: RegisteredDevice }
    | { kind: "bootstrapping"; registered: RegisteredDevice }
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
  // Default on — matches the wording on the Permissions step and the
  // "quietly in the background" pitch. Users who don't want auto-start
  // uncheck it; either way we still kickstart both LaunchAgents on
  // Finish, so setup pays off immediately.
  let runAtLogin = $state(true);
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
      // Resolve the platform-appropriate install root before anything
      // else — every downstream invoke() threads this value through.
      installRoot = await invoke<string>("get_install_root");
      platform = await invoke<string>("get_platform");
      const install = await invoke<CheckInstallResult>("check_install", {
        installRoot,
      });
      bootstrap = { kind: "ready", install };
      // Already-set-up detection: if check_install pulled a device_id
      // out of session_*.json, land on the splash first. User has to
      // explicitly choose Re-register to enter the wizard — protects
      // against accidental clicks from the Applications menu.
      if (install.device_id) {
        mode = "splash";
      }
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

    // Re-register: user picked this on the splash. Delete the old
    // device on Control + wipe local state (session file, models,
    // pipeline state) BEFORE minting a fresh registration key.
    // Skipped when `already` is set — that's the retry path for a
    // registered-but-not-config-written state, where wiping would
    // orphan the new device we just created.
    if (pendingReRegister && !already) {
      finish = { kind: "minting" };
      try {
        await invoke<void>("re_register", {
          installRoot,
          oldDeviceId: pendingReRegister.deviceId,
        });
        pendingReRegister = null;
      } catch (e) {
        finish = {
          kind: "error",
          message: `Re-register cleanup failed: ${e instanceof Error ? e.message : String(e)}`,
        };
        return;
      }
    }

    let registered: RegisteredDevice;
    if (already) {
      registered = already;
    } else {
      // Phase 1: mint a fresh single-use registration key. Fast in
      // steady state; slow when Control's Cloud Run instance cold-starts.
      finish = { kind: "minting" };
      let registrationKey: string;
      try {
        registrationKey = await invoke<string>("mint_registration_key");
      } catch (e) {
        finish = { kind: "error", message: e instanceof Error ? e.message : String(e) };
        return;
      }

      // Phase 2: derive the device name, then redeem the key. The
      // redemption is the call that most often feels slow (Control
      // creates the row, provisions the Zenoh credentials, hands back
      // the AgentConfig).
      let deviceName: string;
      try {
        deviceName = await invoke<string>("suggest_device_name");
      } catch (e) {
        finish = { kind: "error", message: e instanceof Error ? e.message : String(e) };
        return;
      }

      finish = { kind: "registering" };
      try {
        registered = await invoke<RegisteredDevice>("register_device", {
          deviceName,
          registrationKey,
        });
      } catch (e) {
        finish = { kind: "error", message: e instanceof Error ? e.message : String(e) };
        return;
      }
    }

    // Phase 3: write the AgentConfig session file the runtime will
    // read on its next start.
    finish = { kind: "writing", registered };
    let configPath: string | null = null;
    try {
      configPath = await invoke<string>("install_agent_config", {
        installRoot,
        config: registered.config,
      });
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

    // Phase 4: bootstrap the LaunchAgents (register + kickstart). Only
    // errors on truly weird macOS states; best-effort — if this fails
    // the user can still launch Locai Link from Applications later
    // and the companion's kickstart logic recovers.
    finish = { kind: "bootstrapping", registered };
    try {
      await invoke("install_launchagents", {
        installRoot,
        runAtLogin,
      });
    } catch (e) {
      console.warn("install_launchagents failed:", e);
    }

    // Phase 5: queue deploys for any models the user selected. Runs
    // them in parallel — each is an independent HTTP POST + Zenoh
    // command dispatch, so N models take ~1 RTT rather than N × RTT.
    // Failures don't roll back the earlier steps.
    const selected =
      models.kind === "ready"
        ? models.models.filter((m) => models.selected.has(m.id))
        : [];
    finish = {
      kind: "deploying",
      registered,
      done: 0,
      total: selected.length,
    };
    // Pre-register each selected model as "queued 0%" with the local
    // runtime so the companion's Models panel shows every row from t=0
    // rather than one row at a time as the runtime processes deploys
    // serially. Loopback POST — cheap, doesn't gate the real Control
    // dispatch below. Any failure is swallowed inside the Tauri
    // command; the runtime registers the model itself the moment it
    // starts downloading.
    await Promise.allSettled(
      selected.map((m) =>
        invoke<void>("mark_deployment_pending", {
          pipelineId: m.id,
          modelName: m.display_name,
        })
      )
    );
    const results = await Promise.allSettled(
      selected.map((m) =>
        invoke<string>("deploy_model", {
          deviceId: registered.device_id,
          modelId: m.id,
        })
      )
    );
    const failures = results.filter((r) => r.status === "rejected") as PromiseRejectedResult[];
    const deployedCount = results.length - failures.length;

    if (failures.length > 0) {
      // Surface the first failure to keep the error message concise;
      // subsequent failures are usually the same root cause (auth,
      // network) and repeating them adds noise.
      const first = failures[0].reason;
      finish = {
        kind: "error",
        message: `Deploy failed for ${failures.length} of ${selected.length} models: ${
          first instanceof Error ? first.message : String(first)
        }`,
        registered,
      };
      return;
    }

    finish = {
      kind: "done",
      device_id: registered.device_id,
      config_path: configPath,
      deployed_count: deployedCount,
    };
  }

  async function openCompanionPrefs() {
    splashAction = { kind: "working", message: "Opening Preferences…" };
    try {
      await invoke<void>("open_companion_preferences");
      // Preferences window is up in the companion — no need for two
      // windows fighting for focus. Exit the SA cleanly.
      await invoke<void>("exit_app");
    } catch (e) {
      splashAction = {
        kind: "error",
        message: `Couldn't open Preferences: ${e instanceof Error ? e.message : String(e)}`,
      };
    }
  }

  function startReRegister() {
    splashAction = { kind: "confirming", action: "re-register" };
  }

  function startUninstall() {
    splashAction = { kind: "confirming", action: "uninstall" };
  }

  function cancelConfirm() {
    splashAction = { kind: "idle" };
  }

  async function confirmReRegister() {
    if (bootstrap.kind !== "ready" || !bootstrap.install.device_id) return;
    // Stash the old identity for completeSetup() — it invokes
    // `re_register` (Control DELETE + local wipe) before minting a
    // fresh registration key. Wizard runs normally afterwards.
    pendingReRegister = {
      deviceId: bootstrap.install.device_id,
      deviceName: bootstrap.install.device_name,
    };
    splashAction = { kind: "idle" };
    mode = "wizard";
  }

  async function confirmUninstall() {
    splashAction = { kind: "working", message: "Removing Locai Link…" };
    try {
      await invoke<void>("launch_uninstaller_from_sa", { installRoot });
      // Uninstaller runs as a transient user-scope service (cgroup
      // isolated) — it survives us exiting. Close the SA to get out of
      // the way; the tray will disappear as the uninstaller stops the
      // companion service.
      await invoke<void>("exit_app");
    } catch (e) {
      splashAction = {
        kind: "error",
        message: `Uninstall failed: ${e instanceof Error ? e.message : String(e)}`,
      };
    }
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
{:else if mode === "splash" && bootstrap.install.device_id}
  <!-- ============ ALREADY-INSTALLED CHOOSER ============ -->
  <!-- Reuses the wizard's sidebar + stage layout so the "already set
       up" surface looks like the rest of the SA rather than a separate
       app. The steps list is dropped (no step machine to render); the
       brand + version foot stay. -->
  <main class="wizard">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand__tab">SETUP ASSISTANT</div>
        <img class="brand__logo" src={locaiLogo} alt="Locai" />
      </div>
      {#if bootstrap.install.installed && bootstrap.install.version}
        <div class="rail-foot">
          v{bootstrap.install.version}
        </div>
      {/if}
    </aside>

    <section class="content">
      <div class="content__body">
        <p class="eyebrow">MANAGE</p>
        <h1>Already set up on this device</h1>
        <p class="lead">
          {#if bootstrap.install.device_name}
            <strong>{bootstrap.install.device_name}</strong>
            <span class="mono chooser__uuid">· {bootstrap.install.device_id}</span>
          {:else}
            <span class="mono">{bootstrap.install.device_id}</span>
          {/if}
        </p>

        {#if splashAction.kind === "confirming" && splashAction.action === "re-register"}
          <div class="signin-block">
            <p class="lead">
              This removes the current device from Control and clears
              local models. You'll sign in and re-register from scratch.
              The installed app stays.
            </p>
            <div class="row">
              <button class="btn btn--ghost" onclick={cancelConfirm}>Cancel</button>
              <button class="btn btn--danger" onclick={confirmReRegister}>Re-register</button>
            </div>
          </div>
        {:else if splashAction.kind === "confirming" && splashAction.action === "uninstall"}
          <div class="signin-block">
            <p class="lead">
              This stops Locai Link, removes it from your applications,
              and deletes local models. The device row on Control stays
              — remove it from Control if you no longer want it there.
            </p>
            <div class="row">
              <button class="btn btn--ghost" onclick={cancelConfirm}>Cancel</button>
              <button class="btn btn--danger" onclick={confirmUninstall}>Uninstall</button>
            </div>
          </div>
        {:else if splashAction.kind === "working"}
          <div class="signin-block">
            <div class="row">
              <div class="spinner"></div>
              <span>{splashAction.message}</span>
            </div>
          </div>
        {:else}
          {#if splashAction.kind === "error"}
            <div class="signin-block signin-block--error">
              {splashAction.message}
            </div>
          {/if}
          <div class="chooser__actions">
            <button class="btn btn--primary btn--wide" onclick={openCompanionPrefs}>
              Preferences
            </button>
            <button class="btn btn--ghost btn--wide" onclick={startReRegister}>
              Re-register…
            </button>
            <button class="btn btn--ghost btn--wide" onclick={startUninstall}>
              Uninstall…
            </button>
          </div>
        {/if}
      </div>
    </section>
  </main>
{:else}
  <main class="wizard">
    <aside class="sidebar">
      <div class="brand">
        <div class="brand__tab">SETUP ASSISTANT</div>
        <img class="brand__logo" src={locaiLogo} alt="Locai" />
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
            After setup, Locai Link runs in the background and appears
            in your {platform === "linux" ? "system tray" : "menubar"}.
          </p>
          <label class="toggle-row">
            <input
              type="checkbox"
              bind:checked={runAtLogin}
            />
            <div class="toggle-copy">
              <span class="toggle-title">Start Locai Link at login</span>
              <span class="toggle-hint">
                Auto-start the agent and {platform === "linux" ? "tray" : "menubar"} app when you log in.
                Uncheck to launch manually from {platform === "linux" ? "your Applications menu" : "Applications"}.
              </span>
            </div>
          </label>
          <p class="fine-print">
            {#if platform === "linux"}
              You can change this at any time from Locai Link Preferences.
            {:else}
              You can change this later in <strong>System Settings →
              General → Login Items &amp; Extensions</strong>.
            {/if}
          </p>
        {:else if STEPS[current].id === "finish"}
          <p class="eyebrow">STEP 4 · FINISH</p>
          <h1>Register this device</h1>
          <p class="lead">
            Enrol this machine on your Locai organisation and drop the
            initial config in place. The agent picks it up on next start.
          </p>

          {#if finish.kind === "idle"}
            <button
              class="btn btn--primary btn--wide"
              onclick={completeSetup}
            >
              Complete setup
            </button>
          {:else if finish.kind === "minting"}
            <div class="signin-block">
              <div class="row">
                <div class="spinner"></div>
                <span>Requesting registration key…</span>
              </div>
            </div>
          {:else if finish.kind === "registering"}
            <div class="signin-block">
              <div class="row">
                <div class="spinner"></div>
                <span>Registering device with Control…</span>
              </div>
              <p class="fine-print">
                First-time registration can take a few seconds while
                Control provisions credentials for this device.
              </p>
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
          {:else if finish.kind === "bootstrapping"}
            <div class="signin-block">
              <div class="row">
                <div class="spinner"></div>
                <span>Setting up background services…</span>
              </div>
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
                Locai Link is now running in your {platform === "linux" ? "system tray" : "menubar"}.
                You can close this window.
              </p>
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
        <!-- Continue drives the wizard forward; on the last step it's
             replaced by Complete, which exits the app once registration
             finished. Both never render together. -->
        {#if current < STEPS.length - 1}
          <button
            class="btn btn--primary"
            onclick={next}
            disabled={!canContinue}
          >
            Continue
          </button>
        {:else if finish.kind === "done"}
          <button
            class="btn btn--primary"
            onclick={() => void invoke("exit_app")}
          >
            Complete
          </button>
        {/if}
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
  /* Chooser lives inside the wizard shell — stack the 3 lifecycle
     actions vertically, matching the width of the Continue/Sign-in
     buttons in the normal wizard flow. */
  .chooser__actions {
    display: flex;
    flex-direction: column;
    gap: 10px;
    max-width: 320px;
    margin-top: var(--space-16);
  }
  .chooser__uuid {
    color: var(--color-text-muted);
    font-size: 12px;
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

  .brand__logo {
    /* Source PNG is 501×200 (aspect ~2.5:1), designed black-on-light.
       Sidebar is near-black, so invert to render the two-tone
       (black "Loc" + black ".ai" pill with white inner text) as
       (white "Loc" + white pill with black inner text). Preserves
       the design's polarity while making it legible on a dark
       background. Height chosen to sit at the same visual weight
       as the previous 22px text mark. */
    height: 32px;
    width: auto;
    display: block;
    filter: invert(1);
  }

  /* Still used by the splash mark (below) and the "Sign in with Locai"
     button — a green pill wrapping the "ai" letters. Kept alongside
     the new sidebar logo image so those surfaces retain the accent. */
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

  .toggle-row {
    display: flex;
    align-items: flex-start;
    gap: var(--space-12);
    padding: var(--space-10) var(--space-12);
    border: 1px solid var(--color-border-hairline);
    border-radius: var(--radius-md);
    background: var(--color-surface-cream-alt);
    max-width: 40rem;
    cursor: pointer;
    margin: var(--space-8) 0 var(--space-12);
  }
  .toggle-row:hover {
    border-color: var(--color-border-strong);
  }
  .toggle-row input[type="checkbox"] {
    width: 16px;
    height: 16px;
    accent-color: var(--color-primary);
    cursor: pointer;
    flex-shrink: 0;
    margin-top: 2px;
  }
  .toggle-copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .toggle-title {
    font-size: 13px;
    font-weight: var(--weight-semibold);
    color: var(--color-text-strong);
  }
  .toggle-hint {
    font-size: 12px;
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
