from __future__ import annotations

import unittest

from backend import daily_universe


class DailyUniverseRemovalTests(unittest.TestCase):
    def test_daily_stage_is_disabled(self):
        result = daily_universe.snapshot()
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["settings"]["enabled"])
        self.assertEqual(result["symbols"], [])
        self.assertFalse(daily_universe.due())

    def test_install_does_not_wrap_worker_source(self):
        class Core:
            pass

        class Worker:
            marker = object()

        core = Core()
        marker_before = Worker.marker
        status = daily_universe.install(core, Worker)
        self.assertIs(Worker.marker, marker_before)
        self.assertFalse(status["enabled"])
        self.assertEqual(core.daily_master_universe_status()["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
