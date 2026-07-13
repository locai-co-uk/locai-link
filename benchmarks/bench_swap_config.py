# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""llama-swap config generation for a multi-model server."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "plugins" / "language_model"))

from swap_manager import SwapManager  # noqa: E402


def test_write_config(benchmark, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sm = SwapManager(9000, "127.0.0.1", Path("bin"))
    for i in range(3):
        sm._models[f"m{i}"] = {
            "path": f"/models/model-{i}.gguf",
            "args": ["--n-gpu-layers", "35", "--ctx-size", "4096"],
            "env": {"CUDA_VISIBLE_DEVICES": "0"},
            "ttl": 300,
        }
    benchmark(sm._write_config)
