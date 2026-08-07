# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

from pathlib import Path

from link.infra.zenoh import ZenohRouter


def test_router_command_construction(tmp_path):
    """Verify CLI args for the Rust binary."""
    zenoh_dir = tmp_path / "zenoh_home"
    working_dir = tmp_path / "work"
    config_path = tmp_path / "zenoh.json5"

    router = ZenohRouter(
        config_path=config_path,
        zenoh_dir=zenoh_dir,
        working_dir=working_dir,
    )

    cmd = router._get_command()

    # Use 'in' checks to be OS-agnostic (slash vs backslash)
    assert "zenohd" in cmd
    assert "-c" in cmd
    # Verify the command points to our temp config file
    assert str(config_path) in str(cmd)


def test_router_env_vars(tmp_path):
    """Verify environment variables (RocksDB path logic)."""
    data_dir = tmp_path / "data"

    router = ZenohRouter(config_path=Path("conf"), working_dir=data_dir)

    assert router.env_vars["RUST_LOG"] == "info"
    assert str(data_dir) in router.env_vars["ZENOH_BACKEND_ROCKSDB_ROOT"]


def test_router_generated_config(tmp_path):
    """Verify that passing a dict config generates a file."""
    config = {"mode": "router", "endpoints": ["tcp/1.2.3.4:7447"], "storage": {"type": "rocksdb", "dir": "my_db"}}

    zenoh_dir = tmp_path / ".zenoh"

    _router = ZenohRouter(config=config, zenoh_dir=zenoh_dir, working_dir=tmp_path)

    generated_file = zenoh_dir / "generated_router.json5"
    assert generated_file.exists()

    content = generated_file.read_text()
    assert "tcp/1.2.3.4:7447" in content
    assert "my_db" in content


def test_wait_for_router_returns_once_connected(mocker):
    """The readiness wait resolves as soon as a router ZID appears."""
    from link.adapters.zenoh_client import ZenohClient

    client = ZenohClient.__new__(ZenohClient)  # skip config build
    session = mocker.MagicMock()
    # First poll: no routers yet; second poll: one connected.
    session.info().routers_zid.side_effect = [[], ["router-zid-1"]]
    sleep = mocker.patch("link.adapters.zenoh_client.time.sleep")

    client._wait_for_router(session)

    sleep.assert_called_once()  # waited exactly one poll interval


def test_wait_for_router_proceeds_immediately_on_unsupported_probe(mocker):
    """An absent/incompatible probe API (AttributeError/TypeError) proceeds at
    once rather than spinning the whole bound."""
    from link.adapters.zenoh_client import ZenohClient

    client = ZenohClient.__new__(ZenohClient)
    session = mocker.MagicMock()
    session.info.side_effect = AttributeError("no routers_zid")
    sleep = mocker.patch("link.adapters.zenoh_client.time.sleep")

    client._wait_for_router(session)  # returns without raising

    sleep.assert_not_called()


def test_wait_for_router_retries_transient_error_then_succeeds(mocker):
    """A transient probe error is retried until a router appears, not abandoned."""
    from link.adapters.zenoh_client import ZenohClient

    client = ZenohClient.__new__(ZenohClient)
    session = mocker.MagicMock()
    # 1st poll: transient error; 2nd poll: connected.
    session.info().routers_zid.side_effect = [RuntimeError("blip"), ["router-zid-1"]]
    sleep = mocker.patch("link.adapters.zenoh_client.time.sleep")

    client._wait_for_router(session)

    sleep.assert_called_once()  # one retry interval before success


def test_wait_for_router_bounded_when_never_connects(mocker):
    """No router ever appears: return after the bound, do not hang."""
    from link.adapters.zenoh_client import ZenohClient

    client = ZenohClient.__new__(ZenohClient)
    session = mocker.MagicMock()
    session.info().routers_zid.return_value = []
    mocker.patch("link.adapters.zenoh_client.time.sleep")
    # Monotonic clock jumps past the deadline on the second read.
    mocker.patch(
        "link.adapters.zenoh_client.time.monotonic",
        side_effect=[0.0, 0.0, 999.0],
    )

    client._wait_for_router(session)  # returns, no hang


def test_get_session_waits_for_router_after_open(mocker):
    """get_session opens the session, then blocks on the readiness wait before
    returning it — the startup contract that keeps the first report from being
    dropped."""
    import sys

    from link.adapters.zenoh_client import ZenohClient

    fake_session = mocker.MagicMock(name="session")
    fake_zenoh = mocker.MagicMock(name="zenoh")
    fake_zenoh.open.return_value = fake_session
    mocker.patch.dict(sys.modules, {"zenoh": fake_zenoh})

    client = ZenohClient.__new__(ZenohClient)
    client._session = None
    client._zenoh_config = object()  # pyright: ignore[reportAttributeAccessIssue]  # test poke
    wait = mocker.patch.object(client, "_wait_for_router")

    result = client.get_session()

    assert result is fake_session
    fake_zenoh.open.assert_called_once_with(client._zenoh_config)
    wait.assert_called_once_with(fake_session)


def test_wait_for_router_info_method_shape(mocker):
    """zenoh versions where session.info is a method: probe succeeds at once."""
    from link.adapters.zenoh_client import ZenohClient

    client = ZenohClient.__new__(ZenohClient)
    session = mocker.MagicMock()
    session.info.return_value.routers_zid.return_value = ["router-zid-1"]
    sleep = mocker.patch("link.adapters.zenoh_client.time.sleep")

    client._wait_for_router(session)

    sleep.assert_not_called()


def test_wait_for_router_info_property_shape(mocker):
    """zenoh versions where session.info is a property (not callable): the
    probe must not call it, or a TypeError turns the wait into a no-op."""
    from types import SimpleNamespace

    from link.adapters.zenoh_client import ZenohClient

    client = ZenohClient.__new__(ZenohClient)
    info = SimpleNamespace(routers_zid=lambda: ["router-zid-1"])
    session = SimpleNamespace(info=info)
    sleep = mocker.patch("link.adapters.zenoh_client.time.sleep")

    client._wait_for_router(session)

    sleep.assert_not_called()
