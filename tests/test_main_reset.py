# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for `link.main.reset` — its repo-root anchoring and traversal excludes."""

import pytest

from link.main import reset


@pytest.fixture(autouse=True)
def _neutralize_reset_side_effects(  # pyright: ignore[reportUnusedFunction]
    monkeypatch,
):
    """`reset()` calls `stop()` and `ServiceManager(...).uninstall()`
    before its filesystem cleanup — both are wrapped in try/except and
    happen to no-op in the test env today, but leaving them un-mocked
    means a future edit to reset() could silently reach into the host
    (systemd, launchctl, Windows Services). Stub them so these tests
    only exercise the filesystem-walk logic they're actually asserting
    on."""
    monkeypatch.setattr("link.main.stop", lambda *a, **kw: None)

    class _NoopSM:
        def __init__(self, *_a, **_kw) -> None:
            pass

        def uninstall(self, *_a, **_kw) -> None:
            pass

        def is_running(self, *_a, **_kw) -> bool:
            return False

    monkeypatch.setattr("link.main.ServiceManager", _NoopSM)


@pytest.fixture
def link_repo_root(tmp_path, monkeypatch):
    """A tmp dir marked as a locai-link checkout (a pyproject.toml carrying the
    project name) and chdir'd into, so `reset()` anchors its destructive walk
    here. `reset` now refuses to run anywhere it can't positively identify the
    repo root, so every "happy path" test needs this marker in place."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "locai-link"\n')
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_reset_refuses_outside_repo(tmp_path, monkeypatch):
    """Core safety fix: run from a directory that is not a locai-link checkout
    (no marker in cwd or any parent) and reset must refuse — exit non-zero and
    delete nothing — rather than recursively nuking dist/build/.venv from
    wherever it happened to be launched (e.g. $HOME)."""
    monkeypatch.chdir(tmp_path)  # tmp_path deliberately has no pyproject marker

    stray_dist = tmp_path / "dist"
    stray_dist.mkdir()
    (stray_dist / "artifact").write_text("")

    with pytest.raises(SystemExit):
        reset(hard=False)

    assert stray_dist.exists(), "reset must delete nothing when it refuses to run"


def test_reset_anchors_to_repo_root_found_in_parent(tmp_path, monkeypatch):
    """Run from a subdirectory of the checkout: reset should walk up, find the
    marker, and scope deletion to the discovered repo root — neither refusing
    nor escaping above it."""
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "locai-link"\n')
    top_dist = tmp_path / "dist"
    top_dist.mkdir()
    (top_dist / "artifact").write_text("")

    subdir = tmp_path / "src" / "link"
    subdir.mkdir(parents=True)
    monkeypatch.chdir(subdir)

    reset(hard=False)

    assert not top_dist.exists(), "reset should delete dist at the discovered repo root"


def test_reset_does_not_descend_into_node_modules(link_repo_root):
    """Vite (and every other npm package) ships a `dist/` inside its own
    directory under node_modules. `reset` used to walk into node_modules
    and delete those, wrecking every Tauri dev command until `npm
    install` restored them. Pin the fix.
    """
    # Simulate: crates/companion/node_modules/vite/dist/node/cli.js
    vite_dist = link_repo_root / "crates/companion/node_modules/vite/dist/node"
    vite_dist.mkdir(parents=True)
    (vite_dist / "cli.js").write_text("// vite entrypoint")

    # And a target dir from cargo whose subdir happens to be named `build`.
    cargo_build = link_repo_root / "crates/target/debug/build/foo-abc123"
    cargo_build.mkdir(parents=True)
    (cargo_build / "invoked.timestamp").write_text("")

    reset(hard=False)

    assert (vite_dist / "cli.js").exists(), "reset must not delete node_modules/*/dist"
    assert cargo_build.exists(), "reset must not delete target/**/build"


def test_reset_still_deletes_top_level_dist(link_repo_root):
    """The exclude is about *not descending into* node_modules/target —
    ordinary top-level `dist/` at the repo root should still get nuked.
    """
    top_dist = link_repo_root / "dist"
    top_dist.mkdir()
    (top_dist / "artifact.tar.gz").write_text("")

    reset(hard=False)

    assert not top_dist.exists(), "reset should still delete top-level dist"


def test_reset_hard_removes_session_files(link_repo_root):
    """Regression guard on the --hard branch: session_*.json still gets
    nuked when hard=True, and stays put when hard=False.
    """
    configs = link_repo_root / "configs"
    configs.mkdir()
    session = configs / "session_2026-01-01T00-00-00.json"
    session.write_text("{}")

    reset(hard=False)
    assert session.exists(), "soft reset must not touch session_*.json"

    reset(hard=True)
    assert not session.exists(), "hard reset must delete session_*.json"


def test_reset_does_not_touch_repo_metadata_dirs(link_repo_root):
    """.git/.vscode/.github/docs are the historical excludes; ensure the
    new node_modules/target additions didn't accidentally remove them.
    """
    protected = [".git", ".vscode", ".github", "docs"]
    for name in protected:
        d = link_repo_root / name / "dist"
        d.mkdir(parents=True)
        (d / "keep").write_text("")

    reset(hard=False)

    for name in protected:
        keep = link_repo_root / name / "dist" / "keep"
        assert keep.exists(), f"reset should not descend into {name}/"
