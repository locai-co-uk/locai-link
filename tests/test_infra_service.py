from pathlib import Path

import pytest

from link.infra.service import LinuxBackend, MacOSBackend, ServiceManager, WindowsBackend

# --- Linux Tests ---


@pytest.fixture
def mock_linux_env(mocker, tmp_path):
    """Sets up a fake Linux environment."""
    mocker.patch("platform.system", return_value="linux")
    mocker.patch("pathlib.Path.home", return_value=tmp_path / "home")
    mocker.patch("getpass.getuser", return_value="ci_user")

    # Mock subprocess
    mock_run = mocker.patch("link.infra.service._run_cmd")
    return mock_run


def test_factory_returns_linux_backend(mock_linux_env):
    manager = ServiceManager("test-svc")
    assert isinstance(manager, LinuxBackend)


def test_linux_install_generation(mock_linux_env, tmp_path):
    """Verify correct unit file generation."""
    manager = ServiceManager(
        service_name="my-agent", command="/usr/bin/python main.py", description="Test Agent", env_vars={"DEBUG": "1"}
    )

    # Run Install
    manager.install(start_now=True)

    # Verify File
    expected_unit = tmp_path / "home/.config/systemd/user/my-agent.service"
    assert expected_unit.exists()

    content = expected_unit.read_text()
    assert "ExecStart=/usr/bin/python main.py" in content
    assert "Environment=DEBUG=1" in content

    # Verify Systemctl calls
    calls = [str(c[0][0]) for c in mock_linux_env.call_args_list]
    assert any("systemctl --user enable my-agent" in c for c in calls)
    assert any("loginctl enable-linger ci_user" in c for c in calls)


# --- macOS Tests ---


@pytest.fixture
def mock_mac_env(mocker, tmp_path):
    """Sets up a fake macOS environment."""
    mocker.patch("platform.system", return_value="darwin")
    mocker.patch("pathlib.Path.home", return_value=tmp_path / "Users/test")

    mock_run = mocker.patch("link.infra.service._run_cmd")
    # Mock subprocess for is_running check
    mocker.patch("subprocess.run")
    return mock_run


def test_factory_returns_mac_backend(mock_mac_env):
    manager = ServiceManager("test-svc")
    assert isinstance(manager, MacOSBackend)


def test_mac_plist_generation(mock_mac_env, tmp_path):
    """Verify LaunchAgent plist generation."""
    manager = ServiceManager(
        service_name="agent",
        command="/opt/venv/bin/python main.py run",
        description="Mac Agent",
        env_vars={"ENV": "prod"},
    )

    manager.install(start_now=True)

    expected_plist = tmp_path / "Users/test/Library/LaunchAgents/io.locai.agent.plist"
    assert expected_plist.exists()

    content = expected_plist.read_text()
    assert "<string>io.locai.agent</string>" in content
    assert "<string>/opt/venv/bin/python</string>" in content
    assert "<key>ENV</key><string>prod</string>" in content
    assert "<key>RunAtLoad</key> <true/>" in content

    # Verify Launchctl calls
    calls = [str(c[0][0]) for c in mock_mac_env.call_args_list]
    assert any(f"launchctl load {expected_plist}" in c for c in calls)


# --- Windows Tests ---


@pytest.fixture
def mock_windows_env(mocker):
    """Sets up a fake Windows environment."""
    mocker.patch("platform.system", return_value="windows")
    # Mock admin check to allow install to proceed
    mocker.patch("link.infra.service._is_admin", return_value=True)

    mock_run = mocker.patch("link.infra.service._run_cmd")
    return mock_run


def test_factory_returns_windows_backend(mock_windows_env):
    manager = ServiceManager("test-svc")
    assert isinstance(manager, WindowsBackend)


def test_windows_sc_command(mock_windows_env, tmp_path, mocker):
    """Verify SC command generation."""
    # FIX: Use mocker.patch() instead of 'with patch():'
    mocker.patch("pathlib.Path.cwd", return_value=tmp_path)

    manager = ServiceManager(
        service_name="locai-link", command="python.exe main.py", description="Loc.ai Agent", working_dir=str(tmp_path)
    )

    manager.install(start_now=True)

    # Windows commands are constructed as strings passed to _run_cmd
    calls = [str(c[0][0]) for c in mock_windows_env.call_args_list]

    # Check for SC CREATE
    # Note: We check for substrings because paths might vary slightly
    sc_create_call = next((c for c in calls if "sc create locai-link" in c), None)
    assert sc_create_call is not None
    assert 'binPath= "cmd /c \\"python.exe main.py' in sc_create_call
    assert 'displayname= "Loc.ai Agent"' in sc_create_call

    # Check for SC START
    assert any("sc start locai-link" in c for c in calls)


def test_windows_service_env_written_to_registry(mock_windows_env, tmp_path, mocker):
    """env_vars reach the service via the registry Environment value.

    `sc create` cannot set environment variables, so the backend writes the
    REG_MULTI_SZ `Environment` value under the service's registry key. winreg
    is faked through sys.modules so the test runs on any OS.
    """
    import sys

    fake_winreg = mocker.MagicMock()
    mocker.patch.dict(sys.modules, {"winreg": fake_winreg})

    manager = ServiceManager(
        service_name="locai-link",
        command="python.exe main.py",
        description="Loc.ai Agent",
        working_dir=str(tmp_path),
        env_vars={"PYTHONUNBUFFERED": "1"},
    )
    manager.install(start_now=False)

    fake_winreg.OpenKey.assert_called_once()
    set_args = fake_winreg.SetValueEx.call_args.args
    assert set_args[1] == "Environment"
    assert set_args[3] == fake_winreg.REG_MULTI_SZ
    assert set_args[4] == ["PYTHONUNBUFFERED=1"]


# --- New macOS scope + label_prefix + install_all coverage ---


def test_mac_user_scope_writes_to_home(mock_mac_env, tmp_path):
    """Default scope=user lands the plist under ~/Library/LaunchAgents/."""
    manager = ServiceManager(
        service_name="agent",
        command="/opt/locai/locai-link run",
        description="Loc.ai Agent",
    )
    manager.install(start_now=False)
    assert manager.plist_path == tmp_path / "Users/test/Library/LaunchAgents/io.locai.agent.plist"
    assert manager.plist_path.exists()


def test_mac_system_scope_writes_to_library(mock_mac_env):
    """scope=system lands the plist under /Library/LaunchAgents/."""
    manager = ServiceManager(
        service_name="agent",
        command="/Library/Locai/locai-link",
        description="Loc.ai Agent",
        scope="system",
    )
    # Don't actually write — write would need /Library/ root.
    assert manager.plist_path == Path("/Library/LaunchAgents/io.locai.agent.plist")


def test_mac_label_prefix_threads_into_plist(mock_mac_env, tmp_path):
    """Custom label_prefix flows into plist filename, <Label>, and launchctl grep."""
    manager = ServiceManager(
        service_name="agent",
        command="/Library/Locai/locai-link",
        description="Loc.ai Agent",
        label_prefix="uk.co.locai.link",
    )
    manager.install(start_now=False)

    expected_plist = tmp_path / "Users/test/Library/LaunchAgents/uk.co.locai.link.agent.plist"
    assert manager.plist_path == expected_plist
    assert expected_plist.exists()
    content = expected_plist.read_text()
    assert "<string>uk.co.locai.link.agent</string>" in content
    # Old prefix must NOT appear.
    assert "io.locai.agent" not in content


def test_install_all_registers_two_services_in_lockstep(mock_mac_env, tmp_path):
    """install_all() installs every service in the list."""
    from link.infra.service import install_all

    a = ServiceManager(
        service_name="agent",
        command="/Library/Locai/locai-link",
        description="Agent",
        label_prefix="uk.co.locai.link",
    )
    b = ServiceManager(
        service_name="menubar",
        command="/Applications/Locai\\ Link.app/Contents/MacOS/menubar",
        description="Menu-bar",
        label_prefix="uk.co.locai.link",
    )
    install_all([a, b], start_now=True)
    assert a.plist_path.exists()
    assert b.plist_path.exists()


def test_install_all_rolls_back_on_failure(mock_mac_env, tmp_path, mocker):
    """If one install raises, prior installs are uninstalled."""
    from link.infra.service import install_all

    a = ServiceManager(
        service_name="agent",
        command="/Library/Locai/locai-link",
        description="Agent",
        label_prefix="uk.co.locai.link",
    )
    b = ServiceManager(
        service_name="menubar",
        command="/Applications/Locai/menubar",
        description="Menu-bar",
        label_prefix="uk.co.locai.link",
    )
    # Make the second install raise.
    mocker.patch.object(b, "install", side_effect=OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        install_all([a, b], start_now=True)

    # First service should have been uninstalled by the rollback.
    assert not a.plist_path.exists()
