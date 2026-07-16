# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Cross-artifact consistency guards for the macOS whole-app OTA (Linux-runnable).

The UI swap only works if three artifacts agree on where the companion lives and
what launchd runs: the companion LaunchAgent plist, updater._ui_app_destinations,
and the pkg postinstall copy+chown. Nothing at runtime enforces it — drift (e.g.
the plist pointing at /Applications while the OTA writes /Library/Locai) silently
leaves the UI behind. Also guards that a version bump changes the app hash, so
swap_changed_ui_apps actually fires (a stable hash would skip the swap).
"""

from __future__ import annotations

import plistlib
from pathlib import Path, PurePosixPath

import inject_app_hashes

from link.app import updater

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = REPO_ROOT / "bundling" / "pkg"
LAUNCH_AGENTS = PKG / "LaunchAgents"

# Canonical macOS install root — the single value all three artifacts must use.
# PurePosixPath (not Path) so str() yields forward slashes on any host, incl.
# the Windows CI runner, matching the POSIX paths stored in the plists.
MACOS_INSTALL_ROOT = PurePosixPath("/Library/Locai")
COMPANION_LABEL = "uk.co.locai.link.companion"


def _load_plist(name: str) -> dict:
    return plistlib.loads((LAUNCH_AGENTS / name).read_bytes())


def test_companion_launchagent_matches_updater_destination(monkeypatch):
    """The binary launchd starts must be inside the .app the OTA swaps."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    dests = updater._ui_app_destinations("companion", MACOS_INSTALL_ROOT)
    assert dests == [MACOS_INSTALL_ROOT / "Locai Link.app"]

    prog = _load_plist("uk.co.locai.link.companion.plist")["ProgramArguments"]
    assert prog[0] == str(dests[0] / "Contents" / "MacOS" / "Locai Link")


def test_setup_assistant_destination_is_install_root(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    assert updater._ui_app_destinations("setup_assistant", MACOS_INSTALL_ROOT) == [
        MACOS_INSTALL_ROOT / "Setup Assistant.app"
    ]


def test_agent_launchagent_points_at_launcher():
    """The runtime LaunchAgent runs the launcher, which follows `current`."""
    plist = _load_plist("uk.co.locai.link.agent.plist")
    assert plist["ProgramArguments"][0] == str(MACOS_INSTALL_ROOT / "locai-link")
    assert plist["WorkingDirectory"] == str(MACOS_INSTALL_ROOT)


def test_restart_label_matches_companion_plist():
    """updater kickstarts gui/<uid>/<label> — <label> must be the plist's Label."""
    label = _load_plist("uk.co.locai.link.companion.plist")["Label"]
    assert label == COMPANION_LABEL
    src = (REPO_ROOT / "src" / "link" / "app" / "updater.py").read_text(encoding="utf-8")
    assert COMPANION_LABEL in src


def test_payload_names_match_release_workflow(monkeypatch):
    """The names the OTA looks for in the payload must match what release.yml stages."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    assert updater._ui_app_payload_name("companion") == "Locai Link.app"
    assert updater._ui_app_payload_name("setup_assistant") == "Setup Assistant.app"

    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert '"$OTA_ROOT/Locai Link.app"' in release
    assert '"$OTA_ROOT/Setup Assistant.app"' in release


def test_postinstall_makes_install_root_copy_swappable():
    """postinstall must copy the companion to the install root AND hand ownership
    to the user, or the user-context OTA can't replace it (root-owned = swap fails).
    The install-root directory itself must also be user-owned so the swap's
    rename-aside (os.replace within /Library/Locai) is permitted."""
    txt = (PKG / "scripts" / "postinstall").read_text(encoding="utf-8")
    assert 'INSTALL_ROOT="/Library/Locai"' in txt
    assert 'COMPANION_APP="${INSTALL_ROOT}/Locai Link.app"' in txt
    # ownership of the app bundle (so ditto + replace can overwrite it)
    assert 'chown -R "$INSTALL_USER:staff" "$COMPANION_APP"' in txt
    # ownership of the dir itself (so rename-aside within it is allowed)
    assert 'chown "$INSTALL_USER:staff" "$INSTALL_ROOT"' in txt


# ---------------------------------------------------------------------------
# Hash-gating: a version/UI change must change the app hash, or swap_changed_ui_apps
# skips the swap and the UI silently stays behind on an OTA.
# ---------------------------------------------------------------------------


def _fake_companion_tree(root: Path, *, version: str, svelte: str) -> None:
    """Minimal source layout inject_app_hashes walks for the companion."""
    tauri = root / "crates" / "companion" / "src-tauri"
    tauri.mkdir(parents=True)
    (tauri / "tauri.conf.json").write_text(f'{{"version": "{version}"}}\n', encoding="utf-8")
    src = root / "crates" / "companion" / "src"
    src.mkdir(parents=True)
    (src / "App.svelte").write_text(svelte, encoding="utf-8")
    shared = root / "crates" / "shared"
    shared.mkdir(parents=True)
    (shared / "lib.rs").write_text("// shared\n", encoding="utf-8")
    (root / "crates" / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    (root / "crates" / "Cargo.toml").write_text("# workspace\n", encoding="utf-8")


def _companion_hash(root: Path) -> str:
    return inject_app_hashes._hash_sources(root, inject_app_hashes.APP_SOURCES["companion"])


def test_version_bump_changes_companion_hash(tmp_path):
    """release.yml stamps the version into tauri.conf.json before hashing, so the
    hash must differ between versions — otherwise the OTA never swaps the UI."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _fake_companion_tree(a, version="1.0.25", svelte="<main>Link</main>")
    _fake_companion_tree(b, version="1.1.1", svelte="<main>Link</main>")
    assert _companion_hash(a) != _companion_hash(b)


def test_ui_change_changes_companion_hash(tmp_path):
    """An actual UI edit (same version) must also change the hash."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _fake_companion_tree(a, version="1.1.1", svelte="<main>Old Link</main>")
    _fake_companion_tree(b, version="1.1.1", svelte="<main>New Link</main>")
    assert _companion_hash(a) != _companion_hash(b)


def test_identical_source_keeps_stable_hash(tmp_path):
    """Unchanged source keeps a stable hash, so an unchanged app is left running."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    _fake_companion_tree(a, version="1.1.1", svelte="<main>Link</main>")
    _fake_companion_tree(b, version="1.1.1", svelte="<main>Link</main>")
    assert _companion_hash(a) == _companion_hash(b)
