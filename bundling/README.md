# bundling/

Build a self-contained, embeddable distribution of Link via PyInstaller.

## Layout

```
bundling/
├── README.md                  # this file
├── prefetch.py                # downloads native binaries into _artifacts/<platform>/
├── build.py                   # orchestrator: prefetch → pyinstaller
├── locai-link.spec            # PyInstaller spec
├── entitlements.plist         # macOS hardened-runtime entitlements
├── sign_macos.py              # codesign + notarytool + staple (macOS only)
└── _artifacts/                # build inputs (native binaries, generated, gitignored)
    └── <os>-<arch>/
        └── bin-llama/
            ├── llama-server
            └── llama-swap
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
