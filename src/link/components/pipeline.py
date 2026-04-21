# SPDX-FileCopyrightText: 2026 Loc.ai Ltd.
# SPDX-License-Identifier: BUSL-1.1

"""Pipeline thread — connects a source to a sink with cooperative shutdown."""

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)


class Pipeline(threading.Thread):
    """A process that connects a source to a sink."""

    def __init__(self, pipeline_id: str, source: Any, sink: Any):
        """Initialise the pipeline.

        Args:
            pipeline_id (str): The pipeline ID.
            source (Any): The source component.
            sink (Any): The sink component.
        """
        super().__init__(name=f"Pipeline-{pipeline_id}", daemon=True)
        self.pipeline_id = pipeline_id
        self.source = source
        self.sink = sink
        self.running = True
        self.active_event = threading.Event()
        self.active_event.set()

    def run(self):
        """The threaded loop for this specific pipeline.

        Continuously fetches data from source and passes it to sink.
        """
        logger.info(f"Pipeline '{self.pipeline_id}' started.")
        try:
            while self.running:
                if not self.active_event.is_set():
                    # Wait up to 0.5s, but check running flag immediately if woken
                    if self.active_event.wait(timeout=0.5):
                        continue
                    # If timeout expired and still not running, check loop condition
                    if not self.running:
                        break

                # Execute Source -> Sink
                try:
                    data = self.source()
                    if data is not None:
                        if not self.sink(data):
                            logger.warning(f"Sink is returning False, something went wrong '{self.pipeline_id}'")
                    else:
                        # Prevent tight loop if source returns None (e.g. empty queue)
                        time.sleep(0.01)

                except Exception as e:
                    logger.warning(f"Error in pipeline '{self.pipeline_id}': {e}")
                    time.sleep(1)  # Backoff on error

        except Exception as e:
            logger.error(f"Critical pipeline failure '{self.pipeline_id}': {e}")
        finally:
            self._teardown()

    def stop(self):
        """Signals the thread to stop and waits for cleanup."""
        logger.info(f"Stopping pipeline '{self.pipeline_id}'...")
        self.running = False
        self.active_event.clear()

        # We don't join() here to avoid deadlocks if called from inside the thread
        # The run() method's finally block handles teardown.

    def _teardown(self):
        """Releases resources and stops components."""
        logger.info(f"Tearing down pipeline '{self.pipeline_id}'...")
        if hasattr(self.source, "stop"):
            self.source.stop()
        elif hasattr(self.source, "close"):
            self.source.close()

        if hasattr(self.sink, "stop"):
            self.sink.stop()
        elif hasattr(self.sink, "close"):
            self.sink.close()
