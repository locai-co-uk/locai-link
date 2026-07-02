<script lang="ts">
  // The companion is menu-only — this window is hidden on startup and
  // never shown by the current UX. Kept as a working surface so if we
  // ever need a rich secondary view (e.g. "About" or a full details
  // pane) we can wire it in without rebuilding the whole webview
  // scaffold.
</script>

<main>
  <p>Loc.ai Link — menu-bar companion</p>
</main>

<style>
  main {
    display: flex;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    padding: 1rem;
    background: var(--color-paper);
    color: var(--color-ink);
    font-family: var(--font-body);
  }

  p {
    margin: 0;
    color: var(--color-ink-muted);
  }
</style>
