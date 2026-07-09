<script lang="ts">
  // The Preferences window for the Locai Link companion. Tray click
  // on "Preferences…" un-hides + focuses this window; the "close"
  // button on the window hides it (see `on_window_event` in
  // src-tauri/src/lib.rs) so the tray stays running.
  //
  // State model:
  //   - `state` is the full snapshot from `get_prefs_state`, loaded
  //     once on mount. Everything not-agent-status (device identity,
  //     install root, log path) is stable for the window's lifetime.
  //   - `poll_status` refreshes agent state + transport every 4s while
  //     visible so the Status pill and Network panel are live.
  //
  // Failures collapse to a Down agent state; the "Start Locai Link"
  // button is the primary recovery gesture from there.
  import { invoke } from "@tauri-apps/api/core";
  import { onDestroy, onMount } from "svelte";

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
    /// Gates the UI for controls that only exist on macOS today
    /// (Start/Stop/Restart, Start-at-login, Uninstall). On other
    /// platforms those widgets are hidden until the equivalent
    /// service management (systemd, etc.) is wired.
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
  };

  // Models + in-flight deployments are refreshed by the same
  // `poll_status` tick as everything else. Kept outside `prefs` because
  // they aren't part of the initial `get_prefs_state` snapshot — they
  // only exist once the poll has run at least once, and the Models
  // panel handles the empty case naturally.
  let models = $state<ModelInfo[]>([]);
  let deployments = $state<DeploymentProgress[]>([]);

  let prefs = $state<PrefsState | null>(null);
  // Gate the Agent status pill until we've had at least one poll_status
  // confirmation. get_prefs_state's /healthz probe occasionally returns
  // Down on cold start (first WebView open + cold connection pool) even
  // though the runtime is up; without this gate the pill flashed "Stopped"
  // for ~one poll tick before self-correcting.
  let hasPolled = $state(false);
  let loadError = $state<string | null>(null);
  let pending = $state<Set<string>>(new Set());
  let copyFlash = $state<boolean>(false);

  const POLL_INTERVAL_MS = 2000;
  let pollTimer: ReturnType<typeof setInterval> | null = null;

  onMount(async () => {
    // get_prefs_state is now file-reads only (no HTTP), so load completes fast;
    // refreshStatus then makes the single /healthz round-trip that populates
    // status + models. Cold-start UI paints one HTTP RTT after open.
    await load();
    await refreshStatus();
    pollTimer = setInterval(refreshStatus, POLL_INTERVAL_MS);
  });

  onDestroy(() => {
    if (pollTimer) clearInterval(pollTimer);
  });

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
      prefs.agent.status = poll.status;
      prefs.agent.uptime_seconds = poll.uptime_seconds;
      // Version comes from /healthz when Up; when Down, fall back to
      // the value from initial load (resolved via the `current` symlink).
      prefs.agent.version = poll.version ?? prefs.agent.version;
      prefs.network = poll.network;
      models = poll.models;
      deployments = poll.deployments;
      hasPolled = true;
    } catch {
      // Ignore polling errors — next tick tries again.
    }
  }

  // Join models + deployments by pipeline_id so the panel can render
  // one row per pipeline, showing progress inline when a deployment is
  // in flight for that id. Deployments without a matching model row
  // (models list hasn't caught up yet, or the runtime is still creating
  // the pipeline) get their own row keyed by pipeline_id.
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

  async function cancelDeploy(pipelineId: string) {
    await withPending(`cancel:${pipelineId}`, async () => {
      try {
        await invoke<void>("cancel_model_deploy", { pipelineId });
      } catch (e) {
        console.warn("cancel_model_deploy failed:", e);
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
      try {
        await invoke("runtime_stop");
      } catch (e) {
        console.warn("runtime_stop:", e);
      }
      await new Promise((r) => setTimeout(r, 500));
      await refreshStatus();
    });
  }

  async function restartRuntime() {
    await withPending("runtime", async () => {
      try {
        await invoke("runtime_restart");
      } catch (e) {
        console.warn("runtime_restart:", e);
      }
      await new Promise((r) => setTimeout(r, 800));
      await refreshStatus();
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
          <span class="row__value mono">{prefs.agent.version ?? "—"}</span>
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
          {:else}
            <span class="pill pill--{prefs.agent.status}">
              <span class="dot"></span>
              {prefs.agent.status === "up" ? "Running" : "Stopped"}
            </span>
          {/if}
          {#if prefs.agent.status === "up" && prefs.agent.uptime_seconds !== null}
            <span class="uptime">· {formatUptime(prefs.agent.uptime_seconds)}</span>
          {/if}
        </span>
      </div>
      {#if prefs.platform === "macos" || prefs.platform === "linux"}
        <!-- Service management is wired for both macOS (launchctl) and
             Linux (systemctl --user). Windows still lacks a backend,
             so those buttons stay hidden there. -->
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
                      disabled={pending.has(`cancel:${row.pipeline_id}`)}
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
                    disabled={pending.has(`serve:${row.pipeline_id}`)}
                  >
                    Stop
                  </button>
                {:else}
                  <span class="pill pill--idle">✓ Deployed</span>
                  <button
                    class="btn btn--primary btn--sm"
                    onclick={() => toggleServing(row)}
                    disabled={pending.has(`serve:${row.pipeline_id}`)}
                  >
                    Serve
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
      <!-- Uninstall lives on the Setup Assistant chooser (Applications
           menu → Locai Setup Assistant), not here. Preferences is for
           tweaking a running install; lifecycle ops (re-register,
           uninstall) belong on the SA. -->
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
  .toggle-row input[type="checkbox"] {
    width: 15px;
    height: 15px;
    accent-color: var(--color-primary, #00B85A);
    cursor: pointer;
    flex-shrink: 0;
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
