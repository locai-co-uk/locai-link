import pytest

from link.infra.service import LinuxBackend, MacOSBackend, ServiceManager, WindowsBackend

# --- Linux Tests ---


@pytest.fixture
def mock_linux_env(mocker, tmp_path):
    """Sets up a fake Linux environment."""
    mocker.patch("platform.system", return_value="linux")
    mocker.patch("pathlib.Path.home", return_value=tmp_path / "home")
    # FIX: Mock getpass.getuser to avoid CI failures
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
