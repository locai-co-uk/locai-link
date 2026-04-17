# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

import subprocess

import pytest

from link.app import updater

# --- get_local_version ---


def test_get_local_version(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "1.2.3"\n')
    assert updater.get_local_version(tmp_path) == "1.2.3"


def test_get_local_version_missing(tmp_path):
    assert updater.get_local_version(tmp_path) is None


def test_get_local_version_no_version_line(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n')
    assert updater.get_local_version(tmp_path) is None


# --- get_current_branch ---


def test_get_current_branch_success(tmp_path, mocker):
    mock_run = mocker.patch("link.app.updater.subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="dev\n", stderr="")

    assert updater.get_current_branch(tmp_path) == "dev"


def test_get_current_branch_detached_head(tmp_path, mocker):
    mock_run = mocker.patch("link.app.updater.subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="HEAD\n", stderr="")

    assert updater.get_current_branch(tmp_path) is None


def test_get_current_branch_failure(tmp_path, mocker):
    mock_run = mocker.patch("link.app.updater.subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess([], 128, stdout="", stderr="not a repo")

    assert updater.get_current_branch(tmp_path) is None


# --- pull_and_update ---


def test_pull_up_to_date(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="main")

    def fake_run(cmd, **kwargs):
        # fetch: succeeds silently
        if cmd[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        # rev-list: 0 commits behind
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        raise AssertionError(f"Unexpected command: {cmd}")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    assert updater.pull_and_update(tmp_path) is False


def test_pull_behind_clean_tree(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="main")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="3\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    assert updater.pull_and_update(tmp_path) is True

    # Verify key commands were run in order: fetch, rev-list, status, pull, uv install
    fetch_called = any(c[:2] == ["git", "fetch"] for c in calls)
    pull_called = any(c[:2] == ["git", "pull"] for c in calls)
    uv_install_called = any(c[:4] == ["uv", "pip", "install", "-e"] for c in calls)
    stash_called = any("stash" in c for c in calls)

    assert fetch_called
    assert pull_called
    assert uv_install_called
    assert not stash_called, "Should not stash a clean tree"


def test_pull_behind_dirty_tree_stashes(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="main")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="2\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M file.py\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    assert updater.pull_and_update(tmp_path) is True

    stash_push = any(c[:4] == ["git", "stash", "push", "--include-untracked"] for c in calls)
    stash_pop = any(c[:3] == ["git", "stash", "pop"] for c in calls)
    assert stash_push and stash_pop


def test_pull_stash_failure_raises(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="main")

    def fake_run(cmd, **kwargs):
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n", stderr="")
        if cmd[:3] == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M file.py\n", stderr="")
        if cmd[:3] == ["git", "stash", "push"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="conflict")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    with pytest.raises(RuntimeError, match="stash"):
        updater.pull_and_update(tmp_path)


def test_pull_no_git_raises(tmp_path, mocker):
    mocker.patch("link.app.updater._command_exists", return_value=False)

    with pytest.raises(RuntimeError, match="git is required"):
        updater.pull_and_update(tmp_path)


def test_pull_uses_current_branch_over_default(tmp_path, mocker):
    """On a dev branch, the update should pull from origin/dev, not origin/main."""
    mocker.patch("link.app.updater._command_exists", return_value=True)
    mocker.patch("link.app.updater.get_current_branch", return_value="dev")

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:2] == ["git", "rev-list"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    updater.pull_and_update(tmp_path, branch="main")

    # Fetch should target dev, not main
    fetch_cmd = next(c for c in calls if c[:2] == ["git", "fetch"])
    assert fetch_cmd[2:] == ["origin", "dev"]


# --- reinstall_plugin_binaries ---


def test_reinstall_plugin_binaries_no_plugins_dir(tmp_path, mocker):
    mock_run = mocker.patch("link.app.updater.subprocess.run")
    updater.reinstall_plugin_binaries(tmp_path)
    mock_run.assert_not_called()


def test_reinstall_plugin_binaries_runs_each_install_script(tmp_path, mocker):
    plugins = tmp_path / "plugins"
    (plugins / "alpha").mkdir(parents=True)
    (plugins / "alpha" / "install.py").write_text("# alpha")
    (plugins / "bravo").mkdir(parents=True)
    (plugins / "bravo" / "install.py").write_text("# bravo")
    (plugins / "no_installer").mkdir(parents=True)  # Should be skipped

    mock_run = mocker.patch("link.app.updater.subprocess.run")
    mock_run.return_value = subprocess.CompletedProcess([], 0)

    updater.reinstall_plugin_binaries(tmp_path)

    # Should have been called twice (alpha + bravo), skipping no_installer
    assert mock_run.call_count == 2
    called_scripts = [str(call.args[0][-1]) for call in mock_run.call_args_list]
    assert any("alpha" in s for s in called_scripts)
    assert any("bravo" in s for s in called_scripts)


def test_reinstall_plugin_continues_on_failure(tmp_path, mocker):
    """One plugin failing should not stop the others."""
    plugins = tmp_path / "plugins"
    (plugins / "alpha").mkdir(parents=True)
    (plugins / "alpha" / "install.py").write_text("# alpha")
    (plugins / "bravo").mkdir(parents=True)
    (plugins / "bravo" / "install.py").write_text("# bravo")

    def fake_run(cmd, **kwargs):
        if "alpha" in str(cmd):
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0)

    mocker.patch("link.app.updater.subprocess.run", side_effect=fake_run)

    # Should not raise
    updater.reinstall_plugin_binaries(tmp_path)
