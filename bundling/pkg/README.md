<!--
SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
SPDX-License-Identifier: CC0-1.0
-->

# bundling/pkg/

Source files for the macOS `.pkg` installer.

Not built by CI yet — waiting on the **Developer ID Installer**
certificate (see `INSTALLER_PLAN.md`). Once the cert is provisioned
and the workflow lands, the release pipeline will run `pkgbuild` +
`productbuild` against these sources to produce a signed, notarised
`Locai Link.pkg` inside the release `.dmg`.

## What's here

```
bundling/pkg/
├── README.md            ← this file
├── boot.json            ← launcher config (channel, asset_repo, plugin_set)
├── Distribution.xml     ← productbuild layout: which panes, min OS, choices
├── welcome.html         ← Introduction pane content
├── license.html         ← Licence pane content (BUSL-1.1-LOCAI)
├── conclusion.html      ← Summary pane content shown after install
└── scripts/
    └── postinstall      ← Runs after payload copy: chown, symlink CLI, launch Setup Assistant
```

## What's NOT here (yet)

- **The compiled runtime payload.** The release workflow builds the
  Rust launcher + assembles the runtime tree in a staging directory,
  then feeds that directory to `pkgbuild --root` as the payload. This
  directory only holds the *scripts* and *resources* that describe
  the wizard.
- **The Setup Assistant `.app` bundle.** Built by Tauri
  (`crates/setup_assistant/`) and copied into the payload by the
  release workflow.
- **A working build pipeline.** The `pkgbuild` + `productbuild` +
  notarisation invocations live in the release CI once the Installer
  cert is available. Reference commands below.

## Reference build commands (for future CI + local dev on macOS)

```sh
# 1. Assemble the payload tree.
#    Contents end up at /Library/Locai/ on the target.
STAGING=$(mktemp -d)
cp crates/target/release/locai-link                 "$STAGING/locai-link"
cp bundling/pkg/boot.json                           "$STAGING/boot.json"
cp -R crates/target/release/bundle/macos/Setup\ Assistant.app \
                                                    "$STAGING/Setup Assistant.app"

# 2. Build the component .pkg with the postinstall script.
pkgbuild \
    --root "$STAGING" \
    --scripts bundling/pkg/scripts \
    --install-location /Library/Locai \
    --identifier uk.co.locai.link.runtime \
    --version "$VERSION" \
    locai-link-runtime.pkg

# 3. Wrap in the distribution .pkg with the wizard UI.
productbuild \
    --distribution bundling/pkg/Distribution.xml \
    --package-path . \
    --resources bundling/pkg \
    --sign "Developer ID Installer: Loc.ai Ltd (TEAMID)" \
    "Locai Link.pkg"

# 4. Notarise + staple.
xcrun notarytool submit "Locai Link.pkg" --wait --keychain-profile locai-notary
xcrun stapler staple "Locai Link.pkg"
```

## Wizard flow (what the user sees)

1. **Introduction** — `welcome.html` content
2. **Licence** — `license.html`, Agree/Disagree dialog
3. **Destination Select** — Apple's stock disk picker (system-wide only)
4. **Installation** — payload copy progress; `scripts/postinstall`
   runs after copy completes
5. **Summary** — `conclusion.html` content; Setup Assistant has
   already launched by the time this pane appears

Steps 3–5 are Installer.app's stock chrome — we don't restyle them.
See `INSTALLER_PLAN.md` for the design decision.

## Editing copy

- **welcome.html / conclusion.html:** free-form HTML. Keep short, use
  system font stack (`-apple-system, BlinkMacSystemFont, …`) so it
  matches Installer.app's chrome. Design tokens from the mockup can
  be referenced but keep the HTML self-contained (Installer.app
  sandboxes each pane's WebKit view).
- **license.html:** mirrors `LICENSE.md` at the repo root. If the
  licence changes, update both in the same commit.
- **Distribution.xml:** wizard structure and OS/architecture gates.
  The `<pkg-ref version="…">` placeholder is rewritten by the
  release workflow before `productbuild` runs.
