# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Built-in pipeline components: clock, random generator, console sink."""

import logging
import random
import time

from typing_extensions import override

from link.components.registry import ComponentRegistry, Sink, Source

logger = logging.getLogger(__name__)


@ComponentRegistry.register("clock_tick")
class ClockTick(Source):
    """Generates clock ticks (Non-Blocking)."""

    def __init__(self, interval=1.0):
        """Initialises the ClockTick source (interval in seconds)."""
        self.interval = 1.0 / float(interval)
        self.next_tick = time.time()

    @override
    def __call__(self) -> dict[str, float] | None:
        """Returns a {timestamp} tick if the interval has passed, else None (non-blocking)."""
        now = time.time()

        if now < self.next_tick:
            return None  # Yield control back to pipeline loop

        # Catch up, but don't drift if we missed a slot
        self.next_tick = now + self.interval
        return {"timestamp": now}


@ComponentRegistry.register("random_gen")
class RandomGenerator(Source):
    """Generates random numbers (Non-Blocking)."""

    def __init__(self, interval=1.0):
        """Initialises the RandomGenerator source (interval in seconds)."""
        self.interval = 1.0 / float(interval)
        self.next_tick = time.time()

    @override
    def __call__(self) -> dict[str, float] | None:
        """Returns a {val} random number if the interval has passed, else None."""
        now = time.time()

        if now < self.next_tick:
            return None

        self.next_tick = now + self.interval
        return {"val": random.random()}


@ComponentRegistry.register("console")
class ConsolePublisher(Sink):
    """Publishes data to the console."""

    def __init__(self, prefix=""):
        """Initialises ConsolePublisher (prefix prepended to each line)."""
        self.prefix = prefix

    @override
    def __call__(self, data) -> bool | None:
        """Print data to the console; returns True, or None if data is None."""
        if data is not None:
            print(f"{self.prefix}{data}", flush=True)
            return True
        return None
