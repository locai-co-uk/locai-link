# bundling/

Build a self-contained, embeddable distribution of Link via PyInstaller.

## Layout

```
bundling/
├── README.md                  # this file
├── prefetch.py                # downloads native binaries into _artifacts/<platform>/
├── build.py                   # orchestrator: prefetch → pyinstaller → manifest
├── manifest.py                # plugin codes, asset-name derivation, manifest writer
├── locai-link.spec            # PyInstaller spec
├── entitlements.plist         # macOS hardened-runtime entitlements
├── sign_macos.py              # codesign + notarytool + staple (macOS only)
└── _artifacts/                # build inputs (native binaries, generated, gitignored)
    └── <os>-<arch>/
        └── bin-llama/
            ├── llama-server
            └── llama-swap
```

## Building a bundle

A bundle is identified by its plugin set. Pass `--plugins`; the artifact
name is derived. No profile YAMLs, no `--asset-name` override — one knob.

```bash
# LLM-only bundle
uv run python bundling/build.py --plugins language_model

# LLM + transcription
uv run python bundling/build.py --plugins language_model audio_transcriber
```

Bare (zero-plugin) bundles aren't a release shape. For that, install from
source via the curl one-liner in the top-level README.

## Asset naming convention

```
locai-link-<plugin-codes>-<os>-<arch>-v<version>.<ext>
```

Examples:

```
locai-link-llm-linux-x86_64-v1.0.14.tar.gz
locai-link-llm-stt-linux-x86_64-v1.0.14.tar.gz
locai-link-llm-stt-macos-arm64-v1.0.14.tar.gz
locai-link-llm-windows-x86_64-v1.0.14.zip
```

Plugin codes (canonical, in `bundling/manifest.py::PLUGIN_CODES`):

| Plugin | Code |
|---|---|
| `language_model` | `llm` |
| `audio_transcriber` | `stt` |

Codes appear in fixed canonical order (`llm` before `stt`), so two CI runs
of the same plugin set always produce the same name regardless of how the
operator typed `--plugins`.

### Adding a new plugin to the bundleable set

1. Add the plugin → short-code mapping to `PLUGIN_CODES` in `manifest.py`.
2. Add the plugin to `PLUGIN_ORDER` at the position you want it to appear.
3. Done — `--plugins <new>` now works.

Without an entry in `PLUGIN_CODES`, the build hard-fails. This is intentional:
the asset name is part of the public release surface, so adding a plugin
should force a deliberate naming decision.

## Output layout

`bundling/build.py` produces an install_root with an A/B versioned layout
under `dist/locai-link/`:

```
dist/locai-link/                       ← the install_root (this is what gets tarballed)
├── current → versions/<version>       ← symlink; CURRENT pointer file on hosts that can't symlink
├── versions/
│   └── <version>/                     ← the actual PyInstaller bundle
│       ├── locai-link                 ← runtime binary
│       ├── manifest.json
│       ├── _internal/…
│       └── configs/…
└── (launcher binary lands here in Phase 2)
```

Tarballing the whole `dist/locai-link/` directory and extracting it onto a
target machine gives a valid Pattern-A first install — `current` already
points at the seeded version, no bootstrap download needed. See
`../OTA-BUNDLE.md` for the broader update story.

## `manifest.json`

Each built bundle ships a `manifest.json` inside its versioned directory
(`versions/<v>/manifest.json`). Read-only metadata describing what was
built. Not consumed by the running agent (that reads `configs/agent.json`).
Useful for bug reports, telemetry, integrity checks.

| Field | Source |
|---|---|
| `manifest_version` | constant (`1`); bump on schema change |
| `asset_name` | derived from plugins (see above); read by CI for tarball naming |
| `version` | root `pyproject.toml` `[project].version` |
| `git_sha` | short SHA of working tree, or `"unknown"` |
| `built_at` | UTC ISO 8601 timestamp |
| `plugins[]` | `{name, version}` per baked-in plugin |

Inspect it directly:

```bash
cat dist/locai-link/current/manifest.json
```

Native binaries are pre-fetched into `bundling/_artifacts/<os>-<arch>/` as
a build-time side effect (cached across runs so re-builds are fast).

## CI

A GitHub Actions matrix builds the bundle on `macos-latest`, `windows-latest`,
and `ubuntu-latest`, uploading the artefact for each. The macOS job also
codesigns + notarises + staples the bundle when the signing secrets are
configured on the repo (see below); without those secrets the bundle is
uploaded unsigned (PRs from forks land here).

### macOS signing — required repo secrets

Add these to **Settings → Secrets and variables → Actions** before the
signed bundle build will run end-to-end. Missing any one of them causes the
workflow to skip signing and surface a `::notice::` line.

| Secret | Source | Notes |
|---|---|---|
| `APPLE_DEVELOPER_ID_APPLICATION_CERT_P12_BASE64` | Export the **Developer ID Application** cert + private key from Keychain Access as `.p12`, then `base64 < cert.p12 \| pbcopy` | Treat as a long-lived secret; rotates only on cert expiry. |
| `APPLE_DEVELOPER_ID_APPLICATION_CERT_PASSWORD` | Passphrase set during the .p12 export above | — |
| `APPLE_DEVELOPER_ID_APPLICATION` | Common name of the signing identity, e.g. `Developer ID Application: Loc.ai Ltd (TEAMID12AB)` | Find via `security find-identity -v -p codesigning` on a developer Mac. |
| `APPLE_NOTARY_KEY_ID` | App Store Connect → Users and Access → Keys → 10-char key ID | Create the key with **Developer** role; it's the minimum that can submit notarisation. |
| `APPLE_NOTARY_ISSUER_ID` | App Store Connect → Users and Access → Keys → Issuer ID (UUID) | Per-team value, the same for every key issued by that team. |
| `APPLE_NOTARY_KEY_P8_BASE64` | The `.p8` file you download once when creating the API key, base64-encoded | Apple lets you download this once — store it carefully. |

Local dev signing follows the same procedure but reads the cert from your
login keychain instead of importing a .p12:

```bash
export APPLE_DEVELOPER_ID_APPLICATION="Developer ID Application: …"
export APPLE_NOTARY_KEY_ID="ABCD1234EF"
export APPLE_NOTARY_ISSUER_ID="…"
export APPLE_NOTARY_KEY_PATH="$HOME/keys/AuthKey_ABCD1234EF.p8"
uv run python bundling/sign_macos.py --bundle dist/locai-link
```

Pass `--skip-notarise` to sign without submitting to Apple — useful when
iterating on entitlements or codesign flags without burning 10-15 minutes
per round-trip on the notary queue.

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
