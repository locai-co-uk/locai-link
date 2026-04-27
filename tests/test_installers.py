# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""End-to-end tests for the one-line installer scripts."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"
INSTALL_PS1 = ROOT / "install.ps1"
INSTALL_CMD = ROOT / "install.cmd"

# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _build_stub_repo(tmp_path: Path) -> Path:
    """Create a bare git repo whose `main.py` records argv to a JSON file.

    Returns the bare repo path — clone via `file://<bare>`.
    """
    src = tmp_path / "stub_src"
    (src / "src" / "link").mkdir(parents=True)
    (src / "src" / "link" / "__init__.py").write_text("")
    # Stub main.py: writes argv[1:] to LOCAI_TEST_ARGV_FILE and exits 0.
    # Appends so we see both `setup` and `install`/`run` invocations.
    (src / "main.py").write_text(
        "import json, os, pathlib, sys\n"
        "p = pathlib.Path(os.environ['LOCAI_TEST_ARGV_FILE'])\n"
        "log = json.loads(p.read_text()) if p.exists() else []\n"
        "log.append(sys.argv[1:])\n"
        "p.write_text(json.dumps(log))\n"
    )
    (src / "pyproject.toml").write_text("[project]\nname='fake-locai-link'\nversion='0.0.0'\nrequires-python='>=3.9'\n")
    _git("init", "-q", "-b", "main", cwd=src)
    _git("add", "-A", cwd=src)
    _git("commit", "-q", "-m", "init", cwd=src)

    bare = tmp_path / "fake.git"
    subprocess.run(
        ["git", "clone", "--bare", "-q", str(src), str(bare)],
        check=True,
    )
    return bare


def _install_uv_shim(bin_dir: Path) -> None:
    """Drop a fake `uv` on PATH that translates `uv run <script> ...` -> python.

    Avoids the heavy real-uv install path (which would download a Python build
    and create a venv on every test). Both POSIX and Windows variants are
    written so the same fixture serves all platforms.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    py = sys.executable

    # POSIX: `uv` shell script.
    posix = bin_dir / "uv"
    posix.write_text(f'#!/usr/bin/env bash\nif [ "$1" = "run" ]; then\n  shift\n  exec {py} "$@"\nfi\nexit 0\n')
    posix.chmod(posix.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Windows: `uv.cmd` (so `where uv` and `Get-Command uv` both find it).
    (bin_dir / "uv.cmd").write_text(
        "@echo off\r\n"
        'if /I "%~1"=="run" (\r\n'
        "  shift\r\n"
        f'  "{py}" %1 %2 %3 %4 %5 %6 %7 %8 %9\r\n'
        "  exit /b %errorlevel%\r\n"
        ")\r\n"
        "exit /b 0\r\n"
    )


@pytest.fixture
def installer_env(tmp_path: Path):
    """Set up a hermetic install sandbox.

    Yields a dict with:
      - cwd:          empty dir to run the installer from
      - bare:         path to the stub bare repo (LOCAI_REPO_URL target)
      - argv_file:    JSON file the stub main.py appends to
      - env:          os.environ override (PATH-shimmed uv, LOCAI_REPO_URL, ...)
    """
    bare = _build_stub_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    _install_uv_shim(bin_dir)

    cwd = tmp_path / "work"
    cwd.mkdir()
    argv_file = tmp_path / "argv.json"

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["LOCAI_REPO_URL"] = bare.as_uri()
    env["LOCAI_BRANCH"] = "main"
    env["LOCAI_TEST_ARGV_FILE"] = str(argv_file)
    # Force git not to use a system pager / prompt.
    env["GIT_TERMINAL_PROMPT"] = "0"

    yield {"cwd": cwd, "bare": bare, "argv_file": argv_file, "env": env}


def _read_argv(argv_file: Path) -> list[list[str]]:
    assert argv_file.exists(), "stub main.py was never invoked"
    return json.loads(argv_file.read_text())


def _install_invocation(calls: list[list[str]]) -> list[str]:
    """Find the `install` subcommand call among recorded invocations."""
    for call in calls:
        if call and call[0] == "install":
            return call
    raise AssertionError(f"no `install` call recorded; got: {calls}")


# ---------------------------------------------------------------------------
# install.sh
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="bash installer")
def test_install_sh_clones_and_forwards_kebab_args(installer_env):
    """Bash one-liner: clones the repo, invokes `install` with kebab args."""
    env = installer_env["env"]
    cwd = installer_env["cwd"]

    result = subprocess.run(
        [
            "bash",
            str(INSTALL_SH),
            "--device-name",
            "edge-01",
            "--email",
            "user@example.com",
            "--registration-key",
            "REG123",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"install.sh failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    assert (cwd / "locai-link" / ".git").is_dir(), "repo was not cloned"

    install_call = _install_invocation(_read_argv(installer_env["argv_file"]))
    assert "--device-name" in install_call
    assert install_call[install_call.index("--device-name") + 1] == "edge-01"
    assert install_call[install_call.index("--email") + 1] == "user@example.com"
    assert install_call[install_call.index("--registration-key") + 1] == "REG123"


@pytest.mark.skipif(sys.platform == "win32", reason="bash installer")
def test_install_sh_uses_existing_local_repo(installer_env):
    """If invoked from inside an existing checkout, use it instead of cloning."""
    env = installer_env["env"]
    cwd = installer_env["cwd"]

    # Make `cwd` look like a checkout (main.py + src/link present) — use the
    # stub repo's working tree by cloning it once up front.
    subprocess.run(
        ["git", "clone", "-q", env["LOCAI_REPO_URL"], str(cwd / "checkout")],
        check=True,
        env=env,
    )
    work = cwd / "checkout"

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--device-name", "x", "--email", "y@z", "--registration-key", "k"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr

    # Should NOT have created a nested `locai-link/` clone inside `work`.
    assert not (work / "locai-link").exists(), "installer cloned despite local repo present"


# ---------------------------------------------------------------------------
# install.ps1 (PowerShell)
# ---------------------------------------------------------------------------


def _pwsh() -> str | None:
    return shutil.which("pwsh") or (shutil.which("powershell") if sys.platform == "win32" else None)


@pytest.mark.skipif(_pwsh() is None, reason="PowerShell (pwsh) not installed")
def test_install_ps1_translates_pascal_case_args(installer_env):
    """Regression: `-DeviceName foo -Email bar -RegistrationKey baz` must reach
    main.py as `--device-name foo --email bar --registration-key baz`.

    Before the param() block was added to install.ps1, those args slopped into
    the auto `$args` variable and were forwarded raw — argparse rejected them
    with "unrecognized arguments: -DeviceName foo ...". This test pins the
    translation so the regression cannot come back silently.
    """
    pwsh = _pwsh()
    assert pwsh is not None  # narrow Optional[str] for type checker; skipif guards runtime
    env = installer_env["env"]
    cwd = installer_env["cwd"]

    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALL_PS1),
            "-DeviceName",
            "edge-01",
            "-Email",
            "user@example.com",
            "-RegistrationKey",
            "REG123",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, f"install.ps1 failed:\nstdout={result.stdout}\nstderr={result.stderr}"

    install_call = _install_invocation(_read_argv(installer_env["argv_file"]))
    assert "-DeviceName" not in install_call, "PascalCase leaked through to argparse"
    assert install_call[install_call.index("--device-name") + 1] == "edge-01"
    assert install_call[install_call.index("--email") + 1] == "user@example.com"
    assert install_call[install_call.index("--registration-key") + 1] == "REG123"


@pytest.mark.skipif(_pwsh() is None, reason="PowerShell (pwsh) not installed")
def test_install_ps1_switch_flag_forwarded(installer_env):
    """Switch params (`-Dev`, `-StartRunning`) emit bare flags, no value."""
    pwsh = _pwsh()
    assert pwsh is not None  # narrow Optional[str] for type checker; skipif guards runtime
    env = installer_env["env"]
    cwd = installer_env["cwd"]

    result = subprocess.run(
        [
            pwsh,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(INSTALL_PS1),
            "-DeviceName",
            "d",
            "-Email",
            "e@e",
            "-RegistrationKey",
            "k",
            "-StartRunning",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    install_call = _install_invocation(_read_argv(installer_env["argv_file"]))
    assert "--start-running" in install_call


# ---------------------------------------------------------------------------
# install.cmd (Windows batch)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe only on Windows")
def test_install_cmd_forwards_args(installer_env):
    """install.cmd uses `%*` — kebab args should pass through verbatim."""
    env = installer_env["env"]
    cwd = installer_env["cwd"]

    result = subprocess.run(
        [
            "cmd.exe",
            "/c",
            str(INSTALL_CMD),
            "--device-name",
            "edge-cmd",
            "--email",
            "c@d",
            "--registration-key",
            "K",
        ],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr

    install_call = _install_invocation(_read_argv(installer_env["argv_file"]))
    assert install_call[install_call.index("--device-name") + 1] == "edge-cmd"
    assert install_call[install_call.index("--email") + 1] == "c@d"
