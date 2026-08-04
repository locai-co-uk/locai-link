# crates/

Rust workspace housing every native binary shipped with Locai Link:

- **`shared/`** — small helpers (agent-health polling, `boot.json`
  reading, version lookup) consumed by `companion`.
- **`companion/`** — the single `locai-link` binary. Headless
  (`--no-default-features`) it's the supervisor at `<install_root>/locai-link`:
  resolves `current`, execs the versioned runtime, owns Pattern B first-install.
  The default `ui` feature adds the Tauri 2 + Svelte desktop app in-process (the
  menu-bar tray + Preferences via `index.html`, the first-run setup wizard via
  `setup.html`).

## Running the Tauri apps

`cargo tauri ...` isn't available because `tauri-cli` v2.11.4 fails to
build against `rustc 1.95+`. Use the npm-shipped prebuilt instead:

```sh
cd crates/companion
npm install                  # once
npx @tauri-apps/cli dev      # open the dev window
npx @tauri-apps/cli build    # build the .app
```

The Rust side compiles cleanly via plain `cargo`:

```sh
cd crates
cargo build --workspace      # shared, companion
cargo test --workspace       # all tests
```

## Pinned `time` crate

`Cargo.lock` is committed and pins `time = 0.3.49`. `cookie 0.18.1`
(transitive dep of Tauri) calls the pre-0.3.50 `Parsable::parse`
signature. When upstream `cookie` is updated, drop the pin via
`cargo update -p time`.

## Build artefacts

`target/` is at the workspace root (`crates/target/`) — all
crates share one build directory. Gitignored. `node_modules/` and
`dist/` under the Tauri app are gitignored.
