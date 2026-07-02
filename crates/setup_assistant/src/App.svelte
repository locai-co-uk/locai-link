<script lang="ts">
  // Root component. Wizard steps land here as sibling components once
  // the Setup Assistant work begins in earnest.
</script>

<main>
  <h1>Loc.ai Link — Setup Assistant</h1>
  <p>
    Scaffolded. Wizard steps (Confirm device → Models → Serving + ports →
    Permissions → Finish) will replace this shell.
  </p>
</main>

<style>
  main {
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    min-height: 100vh;
    padding: 2rem;
    background: var(--color-paper);
    color: var(--color-ink);
    font-family: var(--font-body);
  }

  h1 {
    font-family: var(--font-display);
    font-weight: 800;
    margin: 0 0 1rem 0;
  }

  p {
    margin: 0;
    color: var(--color-ink-muted);
    text-align: center;
    max-width: 32rem;
    line-height: 1.5;
  }
</style>
