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
