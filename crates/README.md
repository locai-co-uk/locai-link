# crates/

Rust workspace housing every native binary shipped with Locai Link:

- **`launcher/`** — the stable entry-point binary that lives at
  `<install_root>/locai-link` and execs the versioned runtime. Owns
  Pattern B first-install too. Shipped with every Link install.
- **`shared/`** — small helpers (agent-health polling, `boot.json`
  reading, version lookup) consumed by the launcher, Setup Assistant,
  and the menu-bar app.
- **`setup_assistant/`** — Tauri 2 + Svelte first-run app launched by
  the `.pkg` postinstall. Five-step wizard. Closes after registering
  the agent + menu-bar LaunchAgents.
- **`companion/`** — Tauri 2 + Svelte menu-bar app. Tray icon, status
  dot, Models flyout. Long-running.

## Running the Tauri apps

`cargo tauri ...` isn't available because `tauri-cli` v2.11.4 fails to
build against `rustc 1.95+`. Use the npm-shipped prebuilt instead:

```sh
cd crates/setup_assistant   # or companion
npm install                  # once
npx @tauri-apps/cli dev      # open the dev window
npx @tauri-apps/cli build    # build the .app
```

The Rust side compiles cleanly via plain `cargo`:

```sh
cd crates
cargo build --workspace      # all four crates
cargo test --workspace       # all tests
```

## Pinned `time` crate

`Cargo.lock` is committed and pins `time = 0.3.49`. `cookie 0.18.1`
(transitive dep of Tauri) calls the pre-0.3.50 `Parsable::parse`
signature. When upstream `cookie` is updated, drop the pin via
`cargo update -p time`.

## Build artefacts

`target/` is at the workspace root (`crates/target/`) — all four
crates share one build directory. Gitignored. `node_modules/` and
`dist/` under each Tauri app are gitignored.
