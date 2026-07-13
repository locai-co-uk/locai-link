# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Log/telemetry emit cost: record shaping + JSON encode + enqueue."""

import logging
import queue

from link.utils.logger import AsyncHandler


def test_emit_text_log(benchmark):
    # Base AsyncHandler drains via a no-op transport, so this measures emit()
    # (shape -> json.dumps -> put_nowait). Drain before each round so we measure
    # the enqueue-success path, not the queue-full drop path.
    handler = AsyncHandler(templates={"logs": "http://127.0.0.1:9/logs"})
    record = logging.LogRecord(
        "link.system", logging.INFO, __file__, 1, "metrics sample %s", ("cpu=12.3 mem=41.0",), None
    )
    record.category = "metrics"

    def setup():
        try:
            while True:
                handler.queue.get_nowait()
        except queue.Empty:
            pass
        return (record,), {}

    try:
        benchmark.pedantic(handler.emit, setup=setup, rounds=5000, iterations=1)
    finally:
        handler.close()
