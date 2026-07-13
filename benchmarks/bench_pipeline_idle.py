# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Idle pipeline wakeup rate (validates the 2.5 adaptive backoff)."""

import time

from link.components.pipeline import Pipeline


def test_idle_wakeups_bounded():
    calls = {"n": 0}

    def source():
        calls["n"] += 1
        return None

    def sink(_data):
        return True

    p = Pipeline("bench-idle", source, sink)
    p.start()
    time.sleep(1.0)
    p.stop()
    p.join(timeout=2.0)

    n = calls["n"]
    print(f"\nidle source() calls in ~1.0s: {n}")
    # A fixed 10ms spin gives ~100/s; adaptive backoff (cap 200ms) is far lower.
    assert n < 30, f"idle backoff regressed: {n} wakeups in 1s"
