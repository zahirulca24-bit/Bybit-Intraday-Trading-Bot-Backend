from __future__ import annotations

import unittest

from backend import four_hour_directional_pool


class FourHourRemovalTests(unittest.TestCase):
    def test_four_hour_stage_is_disabled(self):
        result = four_hour_directional_pool.snapshot()
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["settings"]["enabled"])
        self.assertEqual(result["symbols"], [])
        self.assertFalse(four_hour_directional_pool.due())

    def test_install_does_not_wrap_worker_source(self):
        class Core:
            pass

        class Worker:
            marker = object()

        core = Core()
        marker_before = Worker.marker
        status = four_hour_directional_pool.install(core, Worker)
        self.assertIs(Worker.marker, marker_before)
        self.assertFalse(status["enabled"])
        self.assertEqual(core.four_hour_directional_pool_status()["status"], "disabled")


if __name__ == "__main__":
    unittest.main()
