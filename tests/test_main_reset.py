# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Tests for `link.main.reset` — specifically its traversal excludes."""

from link.main import reset


def test_reset_does_not_descend_into_node_modules(tmp_path, monkeypatch):
    """Vite (and every other npm package) ships a `dist/` inside its own
    directory under node_modules. `reset` used to walk into node_modules
    and delete those, wrecking every Tauri dev command until `npm
    install` restored them. Pin the fix.
    """
    monkeypatch.chdir(tmp_path)

    # Simulate: crates/companion/node_modules/vite/dist/node/cli.js
    vite_dist = tmp_path / "crates/companion/node_modules/vite/dist/node"
    vite_dist.mkdir(parents=True)
    (vite_dist / "cli.js").write_text("// vite entrypoint")

    # And a target dir from cargo whose subdir happens to be named `build`.
    cargo_build = tmp_path / "crates/target/debug/build/foo-abc123"
    cargo_build.mkdir(parents=True)
    (cargo_build / "invoked.timestamp").write_text("")

    reset(hard=False)

    assert (vite_dist / "cli.js").exists(), "reset must not delete node_modules/*/dist"
    assert cargo_build.exists(), "reset must not delete target/**/build"


def test_reset_still_deletes_top_level_dist(tmp_path, monkeypatch):
    """The exclude is about *not descending into* node_modules/target —
    ordinary top-level `dist/` at the repo root should still get nuked.
    """
    monkeypatch.chdir(tmp_path)

    top_dist = tmp_path / "dist"
    top_dist.mkdir()
    (top_dist / "artifact.tar.gz").write_text("")

    reset(hard=False)

    assert not top_dist.exists(), "reset should still delete top-level dist"


def test_reset_hard_removes_session_files(tmp_path, monkeypatch):
    """Regression guard on the --hard branch: session_*.json still gets
    nuked when hard=True, and stays put when hard=False.
    """
    monkeypatch.chdir(tmp_path)
    configs = tmp_path / "configs"
    configs.mkdir()
    session = configs / "session_2026-01-01T00-00-00.json"
    session.write_text("{}")

    reset(hard=False)
    assert session.exists(), "soft reset must not touch session_*.json"

    reset(hard=True)
    assert not session.exists(), "hard reset must delete session_*.json"


def test_reset_does_not_touch_repo_metadata_dirs(tmp_path, monkeypatch):
    """.git/.vscode/.github/docs are the historical excludes; ensure the
    new node_modules/target additions didn't accidentally remove them.
    """
    monkeypatch.chdir(tmp_path)

    protected = [".git", ".vscode", ".github", "docs"]
    for name in protected:
        d = tmp_path / name / "dist"
        d.mkdir(parents=True)
        (d / "keep").write_text("")

    reset(hard=False)

    for name in protected:
        keep = tmp_path / name / "dist" / "keep"
        assert keep.exists(), f"reset should not descend into {name}/"
