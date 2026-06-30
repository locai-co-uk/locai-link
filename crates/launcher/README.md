# launcher/

Stable Rust launcher for the locai-link runtime. Built once, almost never
updated — its contract is what the OS / host app / service manager starts.

```
<install_root>/
├── locai-link          ← this binary (the launcher)
├── current → versions/<v>
└── versions/<v>/locai-link-runtime    ← what the launcher spawns
```

On start:

1. Resolve `current` symlink (or fall back to a `CURRENT` text-pointer file
   on hosts that can't create symlinks — Windows without Developer Mode).
2. Spawn `<install_root>/versions/<v>/locai-link-runtime` with the
   launcher's argv pass-through.
3. Wait. If the runtime exits with code `42` (the runtime's "restart for
   update" signal, see `../OTA-BUNDLE.md` §5 step 9), re-resolve `current`
   and respawn — an OTA swap may have flipped it. Any other exit code is
   propagated up.

Phase 2 scope: dispatch + restart loop only. The bootstrap branch
(Pattern B, fetch-on-first-use) lands in Phase 3 alongside the
download/verify logic in `src/link/app/bundle_updater.py`. Rollback on
early post-update crashes lands in Phase 4.

## Build

```bash
cd launcher
cargo build --release
# → launcher/target/release/locai-link (or locai-link.exe on Windows)
```

`bundling/build.py` invokes this and copies the resulting binary into
`dist/locai-link/locai-link` — the install_root.

## Test

```bash
cd launcher
cargo test
```

Integration tests in `tests/dispatch.rs` cover symlink dispatch, pointer
file dispatch, exit-42 restart loop, and error paths. The tests are
Unix-only (the stub runtimes are shell scripts); on Windows the launcher
is smoke-tested via `cargo build` in CI.

## Why a separate launcher process at all

A running .exe on Windows is locked; can't be replaced in place. Even on
POSIX, replacing a running binary makes rollback fiddly. The launcher is
a tiny stable shim — kilobytes of code — so the public entry point
doesn't move when the versioned runtime does. See `../OTA-BUNDLE.md` §3
for the full rationale.
