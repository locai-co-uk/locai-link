# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Control-plane read responsiveness during a slow pipeline start (2.1).

`GET /models` (backed by `_snapshot_models`) must stay fast even while a
pipeline start is doing slow work (plugin install, process spawn). Guards
against the read path sharing a lock with the slow write path.
"""

import threading
import time

from link.app.runtime import AgentRuntime
from link.config.models import AgentConfig

_CONFIG = {
    "version": 2.1,
    "identity": {"device_id": "d"},
    "pipelines": [
        {
            "id": "p1",
            "source": {
                "type": "clock_tick",
                "args": {"model_path": "/tmp/x.gguf", "alias": "m", "port": 8100, "host": "127.0.0.1", "mode": "idle"},
            },
            "sink": {"type": "console", "args": {}},
        }
    ],
}

_SLOW = 0.5


def test_snapshot_not_blocked_by_slow_start():
    runtime = AgentRuntime(AgentConfig.model_validate(_CONFIG), None, None)
    in_slow = threading.Event()

    def slow_create(_comp):
        in_slow.set()
        time.sleep(_SLOW)
        return lambda *a: None

    runtime._create_component = slow_create

    t = threading.Thread(target=runtime._start_pipeline, args=("p1",), daemon=True)
    t.start()
    assert in_slow.wait(timeout=2.0), "slow start never entered component creation"

    start = time.perf_counter()
    models = runtime._snapshot_models()
    elapsed = time.perf_counter() - start
    print(f"\n_snapshot_models latency during a slow start: {elapsed * 1000:.1f} ms")

    t.join(timeout=5.0)
    runtime._shutdown()

    assert any(m["id"] == "p1" for m in models)
    assert elapsed < 0.1, f"GET /models blocked {elapsed * 1000:.0f}ms behind a slow start (2.1 contention)"


_STRESS_CONFIG = {
    "version": 2.1,
    "identity": {"device_id": "d"},
    "pipelines": [
        {
            "id": f"s{i}",
            "source": {"type": "clock_tick", "args": {"model_path": f"/tmp/{i}.gguf", "port": 8100 + i}},
            "sink": {"type": "console", "args": {}},
        }
        for i in range(4)
    ],
}


def test_concurrent_start_stop_snapshot_no_deadlock():
    """Hammer start/stop/snapshot concurrently to shake out lock-ordering
    deadlocks or races from the 2.1 lock split. Uses built-in components (no
    plugin install)."""
    runtime = AgentRuntime(AgentConfig.model_validate(_STRESS_CONFIG), None, None)
    ids = [f"s{i}" for i in range(4)]
    stop_flag = threading.Event()
    errors: list[Exception] = []

    def churn():
        n = 0
        while not stop_flag.is_set():
            pid = ids[n % len(ids)]
            try:
                (runtime._start_pipeline if n % 2 == 0 else runtime._stop_pipeline)(pid)
            except Exception as e:  # noqa: BLE001 — record, don't swallow
                errors.append(e)
            n += 1

    def read():
        while not stop_flag.is_set():
            try:
                runtime._snapshot_models()
            except Exception as e:  # noqa: BLE001
                errors.append(e)

    workers = [threading.Thread(target=churn, daemon=True) for _ in range(3)]
    workers += [threading.Thread(target=read, daemon=True) for _ in range(2)]
    for w in workers:
        w.start()
    time.sleep(1.5)
    stop_flag.set()
    for w in workers:
        w.join(timeout=5.0)

    runtime._shutdown()
    assert not any(w.is_alive() for w in workers), "worker stuck — possible deadlock from lock split"
    assert not errors, f"errors during concurrent access: {errors[:3]}"
