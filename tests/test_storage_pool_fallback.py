from __future__ import annotations

"""A leaked or exhausted reader pool must degrade, never deadlock.

Production incident: every reader connection was checked out (holders gone)
and six threads — including the event loop thread — parked forever on
``_reader_condition.wait()``. These tests pin the contract that a caller now
either gets a pooled reader within the budget or a short-lived unpooled read
connection, and that pooled accounting survives the fallback untouched.
"""

import logging
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from digital_life_core.storage.runtime import StorageRuntime

INSTALLATION_ID = "11111111-2222-3333-4444-555555555555"


class ReaderPoolFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = TemporaryDirectory(prefix="companion-v2-pool-")

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def _runtime(self) -> StorageRuntime:
        runtime = StorageRuntime.open(data_root=Path(self._temporary.name) / "v2-data",
                                      installation_id=INSTALLATION_ID)
        runtime._reader_pool_wait_seconds = 0.2
        runtime._reader_pool_size = 1
        return runtime

    def test_exhausted_pool_degrades_to_unpooled_reader(self) -> None:
        runtime = self._runtime()
        entered = threading.Event()
        release = threading.Event()

        def hold_the_only_reader() -> None:
            with runtime._reader_connection():
                entered.set()
                release.wait(10)

        thread = threading.Thread(target=hold_the_only_reader, name="pool-holder")
        thread.start()
        self.assertTrue(entered.wait(10), "holder never acquired the pool reader")
        try:
            started = time.monotonic()
            row = runtime.read_one("SELECT 1 AS ok", ())
            elapsed = time.monotonic() - started
            self.assertIsNotNone(row)
            self.assertEqual(row["ok"], 1)
            self.assertLess(elapsed, 10.0, "read_one blocked instead of degrading")
        finally:
            release.set()
            thread.join(10)

        # The fallback reader must have left the pool accounting untouched.
        with runtime._reader_condition:
            self.assertEqual(runtime._active_readers, 0)
            self.assertEqual(runtime._creating_readers, 0)
            self.assertEqual(len(runtime._all_readers), 1)
            self.assertEqual(len(runtime._idle_readers), 1)
        runtime.close()

    def test_starvation_logs_pool_state_warning(self) -> None:
        runtime = self._runtime()
        entered = threading.Event()
        release = threading.Event()

        def hold_the_only_reader() -> None:
            with runtime._reader_connection():
                entered.set()
                release.wait(10)

        thread = threading.Thread(target=hold_the_only_reader, name="pool-holder")
        thread.start()
        self.assertTrue(entered.wait(10))
        emitted: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: emitted.append(record.getMessage())  # type: ignore[method-assign]
        logging.getLogger("digital_life_core.storage.runtime").addHandler(handler)
        try:
            runtime.read_one("SELECT 1", ())
        finally:
            logging.getLogger("digital_life_core.storage.runtime").removeHandler(handler)
            release.set()
            thread.join(10)
        runtime.close()

        self.assertTrue(
            any("reader pool exhausted" in message for message in emitted),
            f"starvation warning missing: {emitted}",
        )
        self.assertTrue(
            any("pool_size=1" in message for message in emitted),
            f"warning lacks pool state: {emitted}",
        )

    def test_pooled_reader_reusable_after_fallback_release(self) -> None:
        runtime = self._runtime()
        entered = threading.Event()
        release = threading.Event()

        def hold_the_only_reader() -> None:
            with runtime._reader_connection():
                entered.set()
                release.wait(10)

        thread = threading.Thread(target=hold_the_only_reader, name="pool-holder")
        thread.start()
        self.assertTrue(entered.wait(10))
        try:
            first = runtime.read_one("SELECT 1 AS n", ())
            self.assertEqual(first["n"], 1)
        finally:
            release.set()
            thread.join(10)
        # Once the pool is healthy again, reads must go back to pooled mode.
        row = runtime.read_one("SELECT 2 AS n", ())
        self.assertEqual(row["n"], 2)
        with runtime._reader_condition:
            self.assertEqual(len(runtime._all_readers), 1)
            self.assertEqual(len(runtime._idle_readers), 1)
            self.assertEqual(runtime._active_readers, 0)
        runtime.close()


if __name__ == "__main__":
    unittest.main()
