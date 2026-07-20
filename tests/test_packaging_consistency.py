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
from typing import Any

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


def _load_plist(name: str) -> dict[str, Any]:
    return plistlib.loads((LAUNCH_AGENTS / name).read_bytes())


def test_companion_launchagent_matches_updater_destination(monkeypatch):
    """The binary launchd starts must be inside the .app the OTA swaps."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    dests = updater._ui_app_destinations("companion", MACOS_INSTALL_ROOT)  # pyright: ignore[reportArgumentType]
    assert dests == [MACOS_INSTALL_ROOT / "Locai Link.app"]

    # Tauri names the binary after the cargo package, not the productName, so the
    # plist must point at locai-link-companion (correcting this was the INFRA-374 fix).
    prog = _load_plist("uk.co.locai.link.companion.plist")["ProgramArguments"]
    assert prog[0] == str(dests[0] / "Contents" / "MacOS" / "locai-link-companion")


def test_setup_assistant_destination_is_install_root(monkeypatch):
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    assert updater._ui_app_destinations("setup_assistant", MACOS_INSTALL_ROOT) == [  # pyright: ignore[reportArgumentType]
        MACOS_INSTALL_ROOT / "Setup Assistant.app"
    ]


def test_agent_launchagent_points_at_launcher():
    """The runtime LaunchAgent runs the launcher, which follows `current`."""
    plist = _load_plist("uk.co.locai.link.agent.plist")
    assert plist["ProgramArguments"][0] == str(MACOS_INSTALL_ROOT / "locai-link")
    assert plist["WorkingDirectory"] == str(MACOS_INSTALL_ROOT)


def _mock_launchctl(monkeypatch, *, kickstart_rc: int = 0) -> list[list[str]]:
    """Record launchctl/open argvs and make subprocess.run return a chosen rc for
    kickstart, so _restart_companion_macos takes the matching branch."""
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, rc: int) -> None:
            self.returncode = rc

    def _run(argv, **_k):
        calls.append(list(argv))
        rc = kickstart_rc if list(argv[:2]) == ["launchctl", "kickstart"] else 0
        return _Result(rc)

    monkeypatch.setattr(updater.subprocess, "run", _run)
    monkeypatch.setattr(updater.subprocess, "Popen", lambda argv, **_k: calls.append(list(argv)))
    return calls


def test_restart_kickstarts_companion_in_place(monkeypatch):
    """A live companion is kickstarted at gui/<uid>/<the plist Label> — no
    destructive bootout and no LaunchServices fallback on the happy path.
    Asserts the generated launchctl command, not updater source text."""
    label = _load_plist("uk.co.locai.link.companion.plist")["Label"]
    assert label == COMPANION_LABEL

    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater, "_macos_console_uid", lambda: "501")
    calls = _mock_launchctl(monkeypatch, kickstart_rc=0)

    updater._restart_ui_app("companion")

    kicks = [c for c in calls if c[:3] == ["launchctl", "kickstart", "-k"]]
    assert kicks and kicks[0][-1] == f"gui/501/{label}"
    assert not any(c[:2] == ["launchctl", "bootout"] for c in calls)
    assert not any(c and c[0] == "open" for c in calls)


def test_restart_recovers_when_kickstart_misses(monkeypatch):
    """If kickstart can't reach the service, refresh the registration (bootout +
    bootstrap) and retry, then fall back to opening the install-root copy."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater, "_macos_console_uid", lambda: "501")
    # Only the install-root companion exists, so the fallback selects exactly it
    # (path-specific, not blanket-True); assert against the host's own rendering
    # so this holds on Windows too.
    install_app = Path("/Library/Locai/Locai Link.app")
    monkeypatch.setattr(updater.Path, "exists", lambda self: str(self) == str(install_app))
    calls = _mock_launchctl(monkeypatch, kickstart_rc=1)

    updater._restart_ui_app("companion")

    assert any(c[:2] == ["launchctl", "bootout"] for c in calls)
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls)
    # In-place `kickstart -k` first; after re-bootstrap, retry WITHOUT -k so the
    # RunAtLoad spawn isn't raced into a second companion instance.
    kicks = [c for c in calls if c[:2] == ["launchctl", "kickstart"]]
    assert len(kicks) >= 2
    assert kicks[0][:3] == ["launchctl", "kickstart", "-k"]
    assert "-k" not in kicks[1]
    opens = [c for c in calls if c and c[0] == "open"]
    assert opens and opens[0][-1] == str(Path("/Library/Locai/Locai Link.app"))


def test_swap_skips_app_when_staged_signature_fails(monkeypatch, tmp_path):
    """A staged bundle that fails codesign must NOT be installed (so a good live
    app is never overwritten by an unverified one) and must not be marked swapped."""
    monkeypatch.setattr(updater.sys, "platform", "darwin")
    monkeypatch.setattr(updater, "_ui_app_payload_name", lambda key: "Locai Link.app")
    monkeypatch.setattr(updater, "_locate_in_payload", lambda staging, name: tmp_path / "src.app")
    monkeypatch.setattr(updater, "_ui_app_destinations", lambda key, root: [tmp_path / "dest.app"])

    installed: list[Path] = []
    monkeypatch.setattr(updater, "_install_app", lambda src, dest: installed.append(dest))

    def _boom(app):
        raise RuntimeError("codesign verify failed")

    monkeypatch.setattr(updater, "_verify_app_signature", _boom)

    swapped = updater.swap_changed_ui_apps(tmp_path, tmp_path, {"companion": "a"}, {"companion": "b"})
    assert swapped == []
    assert installed == []  # bad staged signature -> never replace the live app


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
