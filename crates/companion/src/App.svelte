<script lang="ts">
  // Preferences window for the Locai Link companion. Tray "Preferences…"
  // un-hides + focuses it; the window's close button hides it (see
  // `on_window_event` in src-tauri/src/lib.rs) so the tray stays running.
  //
  // `state` is the full `get_prefs_state` snapshot, loaded once on mount
  // (device identity, install root, log path — stable for the window's life).
  // `poll_status` refreshes agent state + transport while visible. Failures
  // collapse to a Down state; "Start Locai Link" is the recovery gesture.
  import { invoke } from "@tauri-apps/api/core";
  import { listen, type UnlistenFn } from "@tauri-apps/api/event";
  import { onDestroy, onMount, tick } from "svelte";

  type TransportHealth = {
    type: string;
    endpoint: string | null;
    connected: boolean;
  };

  type DeviceInfo = {
    name: string;
    id: string;
    control_device_url: string;
  };

  type AgentInfo = {
    status: "up" | "down";
    uptime_seconds: number | null;
    version: string | null;
    run_at_login: boolean;
  };

  type AdvancedInfo = {
    log_file: string;
    install_root: string;
  };

  type PrefsState = {
    device: DeviceInfo | null;
    agent: AgentInfo;
    network: TransportHealth | null;
    advanced: AdvancedInfo;
    /// Host OS — "macos" | "linux" | "windows" | …
    /// Gates macOS-only controls (Start/Stop/Restart, Start-at-login,
    /// Uninstall); hidden elsewhere until that service management is wired.
    platform: string;
  };

  type ModelInfo = {
    id: string;
    alias: string;
    port: number | null;
    host: string;
    is_serving: boolean;
  };

  type DeploymentProgress = {
    pipeline_id: string;
    model_name: string | null;
    stage: string;
    progress_pct: number;
  };

  type StatusPoll = {
    status: "up" | "down";
    uptime_seconds: number | null;
    version: string | null;
    network: TransportHealth | null;
    models: ModelInfo[];
    deployments: DeploymentProgress[];
    update_available: boolean;
    latest_version: string | null;
    update_in_flight: boolean;
  };

  type AvailableModel = {
    model_id: string;
    display_name: string;
    framework: string;
    model_type: string;
    size_bytes: number;
    filename_on_server: string;
    file_extension: string;
    is_globally_shared: boolean;
    installed_on_device: boolean;
  };

  type DeployOutcome = {
    command_id: string | null;
    status: string; // "dispatched" | "pending" | "already_installed"
  };

  // Models + in-flight deployments refresh on each `poll_status` tick. Kept
  // outside `prefs` — they aren't in the initial `get_prefs_state` snapshot
  // and only exist once a poll has run; the Models panel handles empty fine.
  let models = $state<ModelInfo[]>([]);
  let deployments = $state<DeploymentProgress[]>([]);
  let updateAvailable = $state(false);
  let latestVersion = $state<string | null>(null);
  // Update-in-progress is inferred client-side (the agent's health server
  // drops during the swap): set on trigger, confirmed once we see it drop,
  // cleared when it returns — success via update_available=false, failure re-shows.
  let updateStarted = $state(false);
  let updateSawDown = $state(false);
  // Suppress the tray-trigger inference during a user-initiated stop/restart
  // so a manual Stop while an update is available isn't mislabelled "Updating".
  let suppressUpdateInfer = $state(false);
  // Authoritative "an OTA swap is applying" flag from the agent, set on trigger
  // by the tray item or the Update button. OR'd with the client-side inference
  // so the window is covered even while the health server is momentarily down.
  let updateInFlight = $state(false);
  const updating = $derived(updateStarted || updateInFlight);

  // Available-models catalog: fetched from Control (device-key authed) on
  // demand, not via the /healthz poll. `requested` tracks just-tapped
  // Downloads for instant feedback before the queued row shows in the poll.
  let availableModels = $state<AvailableModel[]>([]);
  let availableLoading = $state(false);
  let availableError = $state<string | null>(null);
  let availableLoaded = $state(false);
  let requested = $state<Set<string>>(new Set());
  // Model types this build can serve, from supported_model_types (derived from
  // the bundle manifest's plugins). Empty until resolved, and stays empty on
  // failure so we fail closed rather than guessing a capability set.
  let supportedTypes = $state<string[]>([]);
  // Signature of in-flight deployment ids; when it changes (a download starts
  // or finishes) we refresh the catalog so installed/available state is current.
  let lastDeployKeys = "";
  let downloadsSection = $state<HTMLElement | null>(null);

  let prefs = $state<PrefsState | null>(null);
  // Gate the Agent status pill until the first poll_status confirmation:
  // get_prefs_state's cold-start probe can return Down even when the runtime
  // is up, which flashed "Stopped" for one tick before self-correcting.
  let hasPolled = $state(false);
  let loadError = $state<string | null>(null);
  let pending = $state<Set<string>>(new Set());
  let copyFlash = $state<boolean>(false);

  const POLL_INTERVAL_MS = 2000;

  // Release-channel marker shown next to the version. Matches the SA
  // side; read from VITE_CHANNEL at build time (default "alpha" so local
  // dev + missing env still labels correctly). "prod" or empty hides it.
  const CHANNEL = (import.meta.env.VITE_CHANNEL ?? "alpha").toLowerCase();
  const channelLabel =
    CHANNEL === "prod" || CHANNEL === ""
      ? ""
      : `${CHANNEL.charAt(0).toUpperCase()}${CHANNEL.slice(1)}`;
  let pollTimer: ReturnType<typeof setInterval> | null = null;
  let unlistenDownloads: UnlistenFn | null = null;

  onMount(async () => {
    // get_prefs_state is now file-reads only (no HTTP), so load completes fast;
    // refreshStatus then makes the single /healthz round-trip that populates
    // status + models. Cold-start UI paints one HTTP RTT after open.
    await load();
    await refreshStatus();
    try {
      supportedTypes = await invoke<string[]>("supported_model_types");
    } catch (e) {
      // Fail closed: show nothing rather than guessing a capability set.
      console.warn("supported_model_types:", e);
    }
    void loadAvailableModels();
    pollTimer = setInterval(refreshStatus, POLL_INTERVAL_MS);
    // The window is pre-created hidden; refreshStatus bails while hidden, so
    // without this the pill sat on "Checking…" until the next 2s tick after
    // the user opened Preferences. Poll the instant it becomes visible.
    document.addEventListener("visibilitychange", onVisibility);
    // Tray "Download models…" scrolls this window to the catalog section.
    unlistenDownloads = await listen("show-downloads", () => {
      void loadAvailableModels();
      void scrollToDownloads();
    });
  });

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
    document.removeEventListener("visibilitychange", onVisibility);
    unlistenDownloads?.();
  });

  function onVisibility() {
    if (!document.hidden) {
      void refreshStatus();
      void loadAvailableModels();
    }
  }

  async function scrollToDownloads() {
    await tick();
    downloadsSection?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function load() {
    try {
      prefs = await invoke<PrefsState>("get_prefs_state");
      loadError = null;
    } catch (e) {
      loadError = e instanceof Error ? e.message : String(e);
    }
  }

  async function refreshStatus() {
    // Only poll while the window is actually visible — hidden
    // windows keep JS timers alive but there's no user watching.
    if (document.hidden) return;
    try {
      const poll = await invoke<StatusPoll>("poll_status");
      if (!prefs) return;
      const prevStatus = prefs.agent.status;
      const prevUpdateAvail = updateAvailable;
      prefs.agent.status = poll.status;
      prefs.agent.uptime_seconds = poll.uptime_seconds;
      // Version comes from /healthz when Up; when Down, fall back to
      // the value from initial load (resolved via the `current` symlink).
      prefs.agent.version = poll.version ?? prefs.agent.version;
      prefs.network = poll.network;
      models = poll.models;
      deployments = poll.deployments;
      updateAvailable = poll.update_available;
      updateInFlight = poll.update_in_flight;
      // Keep the last-known latest when the probe is Down (returns null).
      latestVersion = poll.latest_version ?? latestVersion;

      // When the set of in-flight deployments changes (a download just started
      // or finished) refresh the catalog so installed/available flags track it.
      const keys = poll.deployments
        .map((d) => d.pipeline_id)
        .sort()
        .join(",");
      if (keys !== lastDeployKeys) {
        lastDeployKeys = keys;
        if (availableLoaded) void loadAvailableModels();
      }

      // Prune optimistic "Starting…" flags once the model is installed locally
      // or has an in-flight deployment row — the poll is now authoritative.
      if (requested.size > 0) {
        const settled = new Set<string>([
          ...poll.models.map((m) => m.id),
          ...poll.deployments.map((d) => d.pipeline_id),
        ]);
        const next = new Set(requested);
        let changed = false;
        for (const id of requested) {
          if (settled.has(id)) {
            next.delete(id);
            changed = true;
          }
        }
        if (changed) requested = next;
      }

      // Infer a tray-triggered update: agent was up with an update available
      // and just dropped, and it wasn't a manual stop/restart.
      if (
        prevStatus === "up" &&
        poll.status === "down" &&
        prevUpdateAvail &&
        !suppressUpdateInfer
      ) {
        updateStarted = true;
      }
      if (updateStarted && poll.status === "down") updateSawDown = true;
      // Once it's back up after dropping, the swap is done: success hides the
      // banner (update_available now false); failure re-shows it to retry.
      if (updateStarted && updateSawDown && poll.status === "up") {
        updateStarted = false;
        updateSawDown = false;
      }
      hasPolled = true;
    } catch {
      // Ignore polling errors — next tick tries again.
    }
  }

  // Join models + deployments by pipeline_id: one row per pipeline, progress
  // inline for in-flight deploys. Deployments with no matching model row
  // (models list lagging, or pipeline still being created) get their own row.
  const modelRows = $derived.by(() => {
    type Row = {
      pipeline_id: string;
      alias: string;
      port: number | null;
      is_serving: boolean;
      deployment: DeploymentProgress | null;
    };
    const byId = new Map<string, Row>();
    for (const m of models) {
      byId.set(m.id, {
        pipeline_id: m.id,
        alias: m.alias,
        port: m.port,
        is_serving: m.is_serving,
        deployment: null,
      });
    }
    for (const d of deployments) {
      const existing = byId.get(d.pipeline_id);
      if (existing) {
        existing.deployment = d;
      } else {
        byId.set(d.pipeline_id, {
          pipeline_id: d.pipeline_id,
          alias: d.model_name ?? d.pipeline_id,
          port: null,
          is_serving: false,
          deployment: d,
        });
      }
    }
    return Array.from(byId.values());
  });

  // Servable iff this build ships a plugin for the model's type.
  // supportedTypes comes from the bundle manifest, so an LLM-only build hides
  // audio/other models the agent can't run.
  function isServable(m: AvailableModel): boolean {
    return supportedTypes.includes(m.model_type);
  }

  // Installed on THIS device. Local /models is authoritative and immediate;
  // Control's installed_on_device flag lags (Firestore round-trip), so prefer
  // local and fall back to the flag. Fixes SA-installed models showing
  // "Download" and finished downloads sticking on "Starting…".
  const installedIds = $derived(new Set(models.map((m) => m.id)));
  function isInstalled(m: AvailableModel): boolean {
    return installedIds.has(m.model_id) || m.installed_on_device;
  }

  // Catalog rows for the download list. Servable types only (see isServable).
  // Models with an in-flight deployment already show in the Models panel above,
  // so hide them here to avoid duplicates; installed models stay, marked below.
  const availableRows = $derived.by(() => {
    const deploying = new Set(deployments.map((d) => d.pipeline_id));
    return availableModels.filter((m) => isServable(m) && !deploying.has(m.model_id));
  });

  async function toggleServing(row: {
    pipeline_id: string;
    is_serving: boolean;
  }) {
    const action = row.is_serving ? "stop-serving" : "serve";
    await withPending(`serve:${row.pipeline_id}`, async () => {
      try {
        await invoke<void>("toggle_model_serving", {
          pipelineId: row.pipeline_id,
          action,
        });
        // Next poll (up to 4s later) reads /models and flips the pill —
        // no optimistic update, poll is authoritative.
      } catch (e) {
        console.warn("toggle_model_serving failed:", e);
      }
    });
  }

  async function loadAvailableModels() {
    availableLoading = true;
    try {
      const list = await invoke<AvailableModel[]>("list_available_models");
      availableModels = list;
      availableError = null;
      availableLoaded = true;
      // Drop optimistic "requested" flags once the model is installed or has a
      // real deployment row; the poll/catalog is now authoritative.
      if (requested.size > 0) {
        const deploying = new Set(deployments.map((d) => d.pipeline_id));
        const next = new Set(requested);
        for (const id of requested) {
          const m = list.find((x) => x.model_id === id);
          if ((m && m.installed_on_device) || deploying.has(id)) next.delete(id);
        }
        requested = next;
      }
    } catch (e) {
      availableError = e instanceof Error ? e.message : String(e);
    } finally {
      availableLoading = false;
    }
  }

  async function requestDeploy(model: AvailableModel) {
    await withPending(`deploy:${model.model_id}`, async () => {
      // Optimistic: show "Starting…" immediately; cleared once the model shows
      // as installed or a deployment row appears.
      requested = new Set(requested).add(model.model_id);
      try {
        const outcome = await invoke<DeployOutcome>("request_model_deploy", {
          modelId: model.model_id,
          modelName: model.filename_on_server || null,
        });
        if (outcome.status === "already_installed") {
          // Nothing queued; refresh so the row flips to "Installed".
          const next = new Set(requested);
          next.delete(model.model_id);
          requested = next;
          await loadAvailableModels();
        }
        // dispatched/pending: the queued row is pre-registered agent-side and
        // the next /healthz poll surfaces progress.
      } catch (e) {
        const next = new Set(requested);
        next.delete(model.model_id);
        requested = next;
        availableError = e instanceof Error ? e.message : String(e);
        console.warn("request_model_deploy failed:", e);
      }
    });
  }

  async function cancelDeploy(pipelineId: string) {
    await withPending(`cancel:${pipelineId}`, async () => {
      try {
        await invoke<void>("cancel_model_deploy", { pipelineId });
      } catch (e) {
        console.warn("cancel_model_deploy failed:", e);
      }
    });
  }

  // Remove the model locally; the runtime stops it first if serving, deletes it,
  // and reports to Control. Re-downloadable afterwards, so no confirm.
  async function uninstallModel(row: { pipeline_id: string }) {
    await withPending(`uninstall:${row.pipeline_id}`, async () => {
      try {
        await invoke<void>("uninstall_model", { pipelineId: row.pipeline_id });
        // Refresh both lists: the deployed row disappears, the model returns to
        // "Available to download".
        await refreshStatus();
        await loadAvailableModels();
      } catch (e) {
        availableError = e instanceof Error ? e.message : String(e);
        console.warn("uninstall_model failed:", e);
      }
    });
  }

  async function withPending<T>(key: string, fn: () => Promise<T>): Promise<void> {
    pending.add(key);
    pending = new Set(pending);
    try {
      await fn();
    } finally {
      pending.delete(key);
      pending = new Set(pending);
    }
  }

  async function startRuntime() {
    await withPending("runtime", async () => {
      try {
        await invoke("runtime_start");
      } catch (e) {
        console.warn("runtime_start:", e);
      }
      // launchctl returns before the process is fully up; give it a
      // beat then refresh so the UI reflects the new state.
      await new Promise((r) => setTimeout(r, 800));
      await refreshStatus();
    });
  }

  async function stopRuntime() {
    await withPending("runtime", async () => {
      suppressUpdateInfer = true;
      try {
        await invoke("runtime_stop");
      } catch (e) {
        console.warn("runtime_stop:", e);
      }
      await new Promise((r) => setTimeout(r, 500));
      await refreshStatus();
      suppressUpdateInfer = false;
    });
  }

  async function restartRuntime() {
    await withPending("runtime", async () => {
      suppressUpdateInfer = true;
      try {
        await invoke("runtime_restart");
      } catch (e) {
        console.warn("runtime_restart:", e);
      }
      await new Promise((r) => setTimeout(r, 800));
      await refreshStatus();
      suppressUpdateInfer = false;
    });
  }

  async function installUpdate() {
    await withPending("update", async () => {
      try {
        await invoke("install_update");
        // The agent swaps the bundle and relaunches; the runtime drops
        // briefly, so surface an in-progress note rather than an error.
        updateStarted = true;
        updateSawDown = false;
      } catch (e) {
        console.warn("install_update:", e);
      }
    });
  }

  async function toggleRunAtLogin(next: boolean) {
    if (!prefs) return;
    const prev = prefs.agent.run_at_login;
    prefs.agent.run_at_login = next;
    try {
      await invoke("set_run_at_login", { enabled: next });
    } catch (e) {
      // Roll back the optimistic flip if the plist edit fails.
      prefs.agent.run_at_login = prev;
      console.warn("set_run_at_login:", e);
    }
  }

  async function revealLog() {
    try {
      await invoke("reveal_log_file");
    } catch (e) {
      console.warn("reveal_log_file:", e);
    }
  }

  async function openControl() {
    try {
      await invoke("open_control_device", {
        deviceId: prefs?.device?.id ?? null,
      });
    } catch (e) {
      console.warn("open_control_device:", e);
    }
  }

  async function copyDeviceId() {
    if (!prefs?.device) return;
    try {
      await navigator.clipboard.writeText(prefs.device!.id);
      copyFlash = true;
      setTimeout(() => {
        copyFlash = false;
      }, 1200);
    } catch {
      // Clipboard denied — silently ignore; the ID is still visible.
    }
  }

  function formatUptime(seconds: number | null): string {
    if (seconds === null || seconds < 0) return "";
    const s = Math.floor(seconds);
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (h > 0) return `${h}h ${m}m`;
    if (m > 0) return `${m}m ${s % 60}s`;
    return `${s}s`;
  }

  function shortId(id: string): string {
    if (id.length <= 12) return id;
    return `${id.slice(0, 8)}…${id.slice(-4)}`;
  }

  function formatSize(bytes: number): string {
    if (!bytes || bytes < 0) return "—";
    const units = ["B", "KB", "MB", "GB", "TB"];
    let v = bytes;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return `${v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
  }

  // Derive a quant tag (e.g. "Q4_K_M") from the asset filename; Control doesn't
  // send it separately. Empty when the filename carries no recognisable tag.
  function deriveQuant(filename: string): string {
    const m = filename.match(/\b(IQ?\d+[A-Z0-9_]*K[A-Z0-9_]*|Q\d+(?:_[A-Z0-9]+)*|F16|F32|BF16)\b/i);
    return m ? m[1].toUpperCase() : "";
  }

  function modelMeta(m: AvailableModel): string {
    const quant = deriveQuant(m.filename_on_server);
    const parts = [formatSize(m.size_bytes)];
    if (quant) parts.push(quant);
    if (m.is_globally_shared) parts.push("Shared");
    return parts.join(" · ");
  }
</script>

<div class="app">
  <header>
    <h1>Preferences</h1>
    <p class="subtitle">Locai Link</p>
  </header>

  {#if loadError}
    <div class="error-card">
      <strong>Couldn't load preferences.</strong>
      <p>{loadError}</p>
      <button class="btn btn--secondary" onclick={load}>Retry</button>
    </div>
  {:else if !prefs}
    <div class="loading">Loading…</div>
  {:else}
    <!-- ============ DEVICE ============ -->
    <section>
      <h2>Device</h2>
      {#if prefs.device}
        <div class="row">
          <span class="row__label">Name</span>
          <span class="row__value">{prefs.device.name}</span>
        </div>
        <div class="row">
          <span class="row__label">ID</span>
          <span class="row__value mono">
            {shortId(prefs.device.id)}
            <button
              class="btn btn--ghost btn--tiny"
              onclick={copyDeviceId}
              title="Copy full ID"
            >
              {copyFlash ? "Copied" : "Copy"}
            </button>
          </span>
        </div>
        <div class="row">
          <span class="row__label">Version</span>
          <span class="row__value mono">
            {prefs.agent.version ?? "—"}{channelLabel ? ` · ${channelLabel}` : ""}
          </span>
        </div>
        <div class="row row--action">
          <button class="btn btn--secondary" onclick={openControl}>
            Open in Control
          </button>
        </div>
      {:else}
        <p class="empty">Device not yet registered.</p>
      {/if}
    </section>

    <!-- ============ AGENT ============ -->
    <section>
      <h2>Agent</h2>
      <div class="row">
        <span class="row__label">Status</span>
        <span class="row__value">
          {#if !hasPolled}
            <span class="pill pill--idle">
              <span class="dot"></span>
              Checking…
            </span>
          {:else if updating}
            <span class="pill pill--updating">
              <span class="dot"></span>
              Updating…
            </span>
          {:else}
            <span class="pill pill--{prefs.agent.status}">
              <span class="dot"></span>
              {prefs.agent.status === "up" ? "Running" : "Stopped"}
            </span>
          {/if}
          {#if !updating && prefs.agent.status === "up" && prefs.agent.uptime_seconds !== null}
            <span class="uptime">· {formatUptime(prefs.agent.uptime_seconds)}</span>
          {/if}
        </span>
      </div>
      {#if updating || (updateAvailable && prefs.agent.status === "up")}
        <div class="update-banner">
          <div class="update-copy">
            <span class="update-title">
              {updating ? "Updating…" : `Update available${latestVersion ? ` · v${latestVersion}` : ""}`}
            </span>
            <span class="update-hint">
              {updating
                ? "Locai Link is installing the update and will restart automatically."
                : "Locai Link will download the new version and restart automatically."}
            </span>
          </div>
          <button
            class="btn btn--primary btn--sm"
            onclick={installUpdate}
            disabled={updating || pending.has("update")}
          >
            {updating ? "Installing…" : "Update now"}
          </button>
        </div>
      {/if}
      {#if (prefs.platform === "macos" || prefs.platform === "linux") && !updating}
        <!-- Service management wired for macOS (launchctl) + Linux
             (systemctl --user); hidden on Windows (no backend) and mid-update
             (the agent bounces on its own). -->
        <div class="row row--action">
          {#if prefs.agent.status === "up"}
            <button
              class="btn btn--secondary"
              onclick={stopRuntime}
              disabled={pending.has("runtime")}
            >
              Stop Locai Link
            </button>
            <button
              class="btn btn--secondary"
              onclick={restartRuntime}
              disabled={pending.has("runtime")}
            >
              Restart
            </button>
          {:else}
            <button
              class="btn btn--primary"
              onclick={startRuntime}
              disabled={pending.has("runtime")}
            >
              Start Locai Link
            </button>
          {/if}
        </div>
        <label class="toggle-row">
          <input
            type="checkbox"
            checked={prefs.agent.run_at_login}
            onchange={(e) => toggleRunAtLogin((e.currentTarget as HTMLInputElement).checked)}
          />
          <div class="toggle-copy">
            <span class="toggle-title">Start Locai Link at login</span>
            <span class="toggle-hint">
              Auto-start the agent and {prefs.platform === "linux" ? "tray" : "menubar"} app when you log in.
            </span>
          </div>
        </label>
      {/if}
    </section>

    <!-- ============ MODELS ============ -->
    <section>
      <h2>Models</h2>
      {#if prefs.agent.status === "down"}
        <p class="empty">Not available while the agent is stopped.</p>
      {:else if modelRows.length === 0}
        <p class="empty">No models on this device yet.</p>
      {:else}
        <ul class="models">
          {#each modelRows as row (row.pipeline_id)}
            <li class="model">
              <div class="model__main">
                <span class="model__alias">{row.alias}</span>
                {#if row.port !== null}
                  <span class="model__port mono">:{row.port}</span>
                {/if}
              </div>
              <div class="model__state">
                {#if row.deployment}
                  <!-- In-flight: SVG progress wheel + stage label -->
                  <span
                    class="wheel"
                    role="progressbar"
                    aria-valuenow={Math.round(row.deployment.progress_pct)}
                    aria-valuemin="0"
                    aria-valuemax="100"
                    title="{row.deployment.stage}"
                  >
                    <svg viewBox="0 0 32 32" width="20" height="20">
                      <circle class="wheel__track" cx="16" cy="16" r="14" />
                      <circle
                        class="wheel__fill"
                        cx="16"
                        cy="16"
                        r="14"
                        stroke-dasharray={`${(row.deployment.progress_pct / 100) * 87.96} 87.96`}
                        transform="rotate(-90 16 16)"
                      />
                    </svg>
                  </span>
                  <span class="model__pct">
                    {Math.round(row.deployment.progress_pct)}%
                  </span>
                  <span class="model__stage">
                    {row.deployment.stage === "downloading"
                      ? "Downloading"
                      : row.deployment.stage === "configuring"
                        ? "Configuring"
                        : row.deployment.stage === "queued"
                          ? "Queued"
                          : row.deployment.stage}
                  </span>
                  {#if row.deployment.stage === "downloading"}
                    <button
                      class="btn btn--ghost btn--sm"
                      onclick={() => cancelDeploy(row.pipeline_id)}
                      disabled={updating || pending.has(`cancel:${row.pipeline_id}`)}
                      aria-label={`Cancel download of ${row.alias}`}
                    >
                      Cancel
                    </button>
                  {/if}
                {:else if row.is_serving}
                  <span class="pill pill--up">▶ Serving</span>
                  <button
                    class="btn btn--ghost btn--sm"
                    onclick={() => toggleServing(row)}
                    disabled={updating || pending.has(`serve:${row.pipeline_id}`)}
                  >
                    Stop
                  </button>
                  <button
                    class="btn btn--ghost btn--sm"
                    onclick={() => uninstallModel(row)}
                    disabled={updating || pending.has(`uninstall:${row.pipeline_id}`)}
                    aria-label={`Remove ${row.alias}`}
                  >
                    Remove
                  </button>
                {:else}
                  <span class="pill pill--idle">✓ Deployed</span>
                  <button
                    class="btn btn--primary btn--sm"
                    onclick={() => toggleServing(row)}
                    disabled={updating || pending.has(`serve:${row.pipeline_id}`)}
                  >
                    Serve
                  </button>
                  <button
                    class="btn btn--ghost btn--sm"
                    onclick={() => uninstallModel(row)}
                    disabled={updating || pending.has(`uninstall:${row.pipeline_id}`)}
                    aria-label={`Remove ${row.alias}`}
                  >
                    Remove
                  </button>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <!-- ============ AVAILABLE MODELS (download) ============ -->
    <section bind:this={downloadsSection}>
      <div class="section-head">
        <h2>Available models</h2>
        <button
          class="btn btn--ghost btn--tiny"
          onclick={loadAvailableModels}
          disabled={availableLoading}
        >
          {availableLoading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      {#if availableError}
        <p class="empty error-text">{availableError}</p>
      {/if}
      {#if !availableLoaded && availableLoading}
        <p class="empty">Loading models…</p>
      {:else if availableRows.length === 0}
        <p class="empty">
          {availableError ? "Couldn't load your model library." : "No models available to download."}
        </p>
      {:else}
        <ul class="models">
          {#each availableRows as model (model.model_id)}
            <li class="model">
              <div class="model__main">
                <span class="model__alias">{model.display_name}</span>
                <span class="model__meta">{modelMeta(model)}</span>
              </div>
              <div class="model__state">
                {#if isInstalled(model)}
                  <span class="pill pill--idle">✓ Installed</span>
                {:else if requested.has(model.model_id)}
                  <span class="pill pill--idle">Starting…</span>
                {:else}
                  <button
                    class="btn btn--primary btn--sm"
                    onclick={() => requestDeploy(model)}
                    disabled={updating || pending.has(`deploy:${model.model_id}`) || prefs.agent.status === "down"}
                    title={prefs.agent.status === "down" ? "Start Locai Link to download models" : ""}
                  >
                    Download
                  </button>
                {/if}
              </div>
            </li>
          {/each}
        </ul>
      {/if}
    </section>

    <!-- ============ NETWORK ============ -->
    <section>
      <h2>Network</h2>
      {#if prefs.agent.status === "down"}
        <p class="empty">Not available while the agent is stopped.</p>
      {:else if prefs.network}
        <div class="row">
          <span class="row__label">Endpoint</span>
          <span class="row__value mono">{prefs.network.endpoint ?? "—"}</span>
        </div>
        <div class="row">
          <span class="row__label">Status</span>
          <span class="row__value">
            <span class="pill pill--{prefs.network.connected ? 'up' : 'down'}">
              <span class="dot"></span>
              {prefs.network.connected ? "Connected" : "Disconnected"}
            </span>
          </span>
        </div>
      {:else}
        <p class="empty">No transport configured.</p>
      {/if}
    </section>

    <!-- ============ ADVANCED ============ -->
    <section>
      <h2>Advanced</h2>
      <div class="row">
        <span class="row__label">Log file</span>
        <span class="row__value mono short">{prefs.advanced.log_file}</span>
      </div>
      <div class="row row--action">
        <button class="btn btn--secondary" onclick={revealLog}>
          {prefs.platform === "macos" ? "Reveal in Finder" : "Open folder"}
        </button>
      </div>
      <div class="row">
        <span class="row__label">Install root</span>
        <span class="row__value mono">{prefs.advanced.install_root}</span>
      </div>
      <!-- Uninstall lives on the Setup Assistant, not here: Preferences tweaks
           a running install; lifecycle ops (re-register, uninstall) go to the SA. -->
    </section>
  {/if}
</div>

<style>
  :global(html, body) {
    height: 100%;
  }
  :global(body) {
    margin: 0;
    font-family: var(--font-body, -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif);
    background: var(--color-surface-cream, #FAF9F6);
    color: var(--color-text, #1A1A1A);
    -webkit-font-smoothing: antialiased;
  }

  .app {
    padding: 20px 24px 32px;
    max-width: 640px;
    margin: 0 auto;
  }

  header {
    margin-bottom: 20px;
  }
  h1 {
    font-size: 22px;
    font-weight: 600;
    margin: 0 0 2px;
    color: var(--color-text-strong, #0A0A0A);
  }
  .subtitle {
    font-size: 12px;
    color: var(--color-text-muted, #8A877F);
    margin: 0;
  }

  section {
    background: var(--color-surface, #FFFFFF);
    border: 1px solid var(--color-border-hairline, #ECEAE4);
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 14px;
  }
  h2 {
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: var(--color-text-muted, #8A877F);
    margin: 0 0 10px;
  }

  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 6px 0;
    font-size: 13px;
    min-height: 24px;
  }
  .row__label {
    color: var(--color-text-secondary, #46443F);
    flex-shrink: 0;
  }
  .row__value {
    color: var(--color-text-strong, #0A0A0A);
    display: inline-flex;
    align-items: center;
    gap: 8px;
    text-align: right;
    min-width: 0;
  }
  .row__value.mono {
    font-family: var(--font-mono, ui-monospace, SF Mono, Menlo, monospace);
    font-size: 12px;
  }
  .row__value.short {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    max-width: 260px;
    direction: rtl;
    text-align: left;
  }
  .row--action {
    justify-content: flex-start;
    gap: 8px;
    padding: 6px 0 2px;
  }

  .pill {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 500;
  }
  .pill--up {
    background: var(--color-surface-tint-green-3, #EAF9F1);
    color: var(--color-primary-pressed, #00A852);
  }
  .pill--down {
    background: var(--color-surface-tint-error, #FCECEA);
    color: var(--color-error, #E84D3D);
  }
  .pill--idle {
    background: var(--color-surface-alt, #F1F0EA);
    color: var(--color-text-muted, #8A877F);
  }
  .pill--updating {
    background: var(--color-surface-tint-green-3, #EAF9F1);
    color: var(--color-primary-pressed, #00A852);
  }
  .pill--updating .dot {
    animation: pulse 1.2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  /* Models panel */
  .models {
    list-style: none;
    padding: 0;
    margin: 0;
    display: flex;
    flex-direction: column;
    gap: 6px;
  }
  .model {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 12px;
    background: var(--color-surface-alt, #F7F5EF);
    border-radius: 8px;
    gap: 12px;
  }
  .model__main {
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
    flex: 1;
  }
  .model__alias {
    font-weight: 500;
    color: var(--color-text, #2C2A24);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .model__port {
    color: var(--color-text-muted, #8A877F);
    font-size: 12px;
  }
  .model__meta {
    color: var(--color-text-muted, #8A877F);
    font-size: 12px;
    white-space: nowrap;
  }

  .section-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
  }
  /* h2 inside a section-head keeps its bottom margin as the list's top gap. */
  .error-text {
    color: var(--color-error, #E84D3D);
  }
  .model__state {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
  }
  .model__pct {
    font-variant-numeric: tabular-nums;
    font-size: 13px;
    color: var(--color-text, #2C2A24);
    min-width: 3.5ch;
    text-align: right;
  }
  .model__stage {
    font-size: 12px;
    color: var(--color-text-muted, #8A877F);
  }
  .wheel {
    display: inline-flex;
  }
  .wheel__track {
    fill: none;
    stroke: var(--color-surface-tint-3, #E9E6DE);
    stroke-width: 4;
  }
  .wheel__fill {
    fill: none;
    stroke: var(--color-primary, #00C05F);
    stroke-width: 4;
    stroke-linecap: round;
    transition: stroke-dasharray 0.4s ease-out;
  }
  .btn--sm {
    padding: 4px 10px;
    font-size: 12px;
  }
  .dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
    display: inline-block;
    flex-shrink: 0;
  }
  .uptime {
    color: var(--color-text-muted, #8A877F);
    font-size: 12px;
  }

  .update-banner {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 10px 12px;
    margin-top: 8px;
    border: 1px solid var(--color-primary-pressed, #00A852);
    background: var(--color-surface-tint-green-3, #EAF9F1);
    border-radius: 8px;
  }
  .update-copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 0;
  }
  .update-title {
    font-size: 13px;
    font-weight: 600;
    color: var(--color-primary-pressed, #00A852);
  }
  .update-hint {
    font-size: 12px;
    color: var(--color-text-secondary, #46443F);
  }

  .toggle-row {
    display: flex;
    align-items: flex-start;
    gap: 12px;
    padding: 10px;
    border: 1px solid var(--color-border-hairline, #ECEAE4);
    border-radius: 8px;
    background: var(--color-surface-cream-alt, #FBFAF7);
    cursor: pointer;
    margin-top: 8px;
  }
  /* Custom checkbox — appearance:none so it renders identically on
     webkit2gtk and WKWebView, in light and dark. Colors come from tokens
     that flip with the theme, so no per-mode overrides are needed. */
  :global(input[type="checkbox"]) {
    appearance: none;
    -webkit-appearance: none;
    position: relative;
    width: 16px;
    height: 16px;
    margin: 0;
    flex-shrink: 0;
    cursor: pointer;
    border: 1.5px solid var(--color-border-checkbox-off, #C9C6BE);
    border-radius: var(--radius-checkbox, 5px);
    background: var(--color-surface, #FFFFFF);
    transition: background 120ms ease, border-color 120ms ease;
  }
  :global(input[type="checkbox"]:checked) {
    background: var(--color-primary, #00B85A);
    border-color: var(--color-primary, #00B85A);
  }
  /* Checkmark — a rotated border shown only when checked. The on-dark
     token stays light in both themes, so it reads on the green fill. */
  :global(input[type="checkbox"]:checked)::after {
    content: "";
    position: absolute;
    left: 4.5px;
    top: 1px;
    width: 4px;
    height: 8px;
    border: solid var(--color-text-on-dark, #FFFFFF);
    border-width: 0 2px 2px 0;
    transform: rotate(45deg);
  }
  .toggle-row input[type="checkbox"] {
    width: 15px;
    height: 15px;
    margin-top: 1px;
  }
  .toggle-copy {
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .toggle-title {
    font-size: 13px;
    font-weight: 500;
    color: var(--color-text-strong, #0A0A0A);
  }
  .toggle-hint {
    font-size: 12px;
    color: var(--color-text-muted, #8A877F);
  }

  .btn {
    font-family: inherit;
    font-size: 12px;
    font-weight: 500;
    padding: 5px 12px;
    border-radius: 6px;
    border: 1px solid transparent;
    cursor: pointer;
    background: transparent;
    color: inherit;
    line-height: 1.4;
    transition: background 120ms, border-color 120ms;
  }
  .btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .btn--primary {
    background: var(--color-primary, #00B85A);
    /* White on green — same in both themes because the button bg
       is always the brand green, not a themed surface. */
    color: #fff;
    border-color: var(--color-primary-pressed, #00A852);
  }
  .btn--primary:hover:not(:disabled) {
    background: var(--color-primary-pressed, #00A852);
  }
  .btn--secondary {
    background: var(--color-surface, #fff);
    color: var(--color-text-strong, #0A0A0A);
    border-color: var(--color-border-input, #D9D6CF);
  }
  .btn--secondary:hover:not(:disabled) {
    background: var(--color-surface-hover, #F5F4F1);
  }
  .btn--danger {
    background: transparent;
    color: var(--color-error, #E84D3D);
    border-color: var(--color-error, #E84D3D);
  }
  .btn--danger:hover:not(:disabled) {
    background: var(--color-surface-tint-error, #FCECEA);
  }
  .btn--ghost {
    color: var(--color-text-muted, #8A877F);
    background: transparent;
    border: 1px solid transparent;
  }
  .btn--ghost:hover:not(:disabled) {
    color: var(--color-text-strong, #0A0A0A);
    background: var(--color-surface-hover, #F5F4F1);
  }
  .btn--tiny {
    font-size: 11px;
    padding: 2px 8px;
  }

  .empty {
    color: var(--color-text-muted, #8A877F);
    font-size: 13px;
    margin: 4px 0 0;
  }

  .loading {
    padding: 20px;
    text-align: center;
    color: var(--color-text-muted, #8A877F);
    font-size: 13px;
  }

  .error-card {
    padding: 12px 14px;
    border: 1px solid var(--color-border-error, #F5C5BF);
    background: var(--color-surface-tint-error, #FCECEA);
    border-radius: 8px;
    color: var(--color-error, #E84D3D);
    font-size: 13px;
  }
  .error-card p {
    margin: 6px 0 10px;
  }
</style>
