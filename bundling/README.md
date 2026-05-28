# bundling/

Build a self-contained, embeddable distribution of Link via PyInstaller.

## Layout

```
bundling/
├── README.md                  # this file
├── prefetch.py                # downloads native binaries into _artifacts/<platform>/
├── _artifacts/                # build inputs (native binaries, generated, gitignored)
│   └── <os>-<arch>/
│       └── bin-llama/
│           ├── llama-server
│           └── llama-swap
└── (forthcoming)
    ├── locai-link.spec        # PyInstaller spec
    ├── build.py               # orchestrator: prefetch → pyinstaller → post-sign
    ├── hooks/                 # PyInstaller hidden-import hooks
    └── entitlements.plist     # macOS hardened-runtime entitlements
```

## Local build

```bash
# Build a Meetily-shaped bundle (LLM + transcription)
uv run python bundling/build.py --plugins language_model audio_transcriber

# Or include every known plugin (avoid for partner bundles)
uv run python bundling/build.py --all-plugins
```

Plugin selection is **explicit by design** — bundles are partner-scoped, so
only the plugins listed get installed, have their dist-info collected, and
contribute hidden imports / native binaries.  Run without arguments to see
the known-plugin list.

Output goes to `dist/locai-link/`.  Native binaries are pre-fetched into
`bundling/_artifacts/<os>-<arch>/` as a build-time side effect (cached
across runs so re-builds are fast).

## CI

A GitHub Actions matrix builds the bundle on `macos-latest`, `windows-latest`,
and `ubuntu-latest`, uploading the artefact for each. Signing/notarisation is
layered on macOS/Windows in a separate post-build step.

## What gets bundled

- Python interpreter + every runtime dependency from the root `pyproject.toml`.
- `plugins/` source plus their installed entry-point metadata.
- Native binaries fetched by `prefetch.py` (`llama-server`, `llama-swap`,
  and their shared libraries). These would normally be downloaded at first run
  by `plugins/language_model/install.py`; in a frozen bundle the install path
  is skipped and these pre-bundled binaries are used directly.

## What does NOT get bundled

- Model files (`.gguf` etc.). Models are downloaded to the user's data dir at
  runtime — they're per-device and far too large to ship.
- Cloud credentials, registration keys, session state.
