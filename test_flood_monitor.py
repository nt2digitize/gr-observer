"""Offline tests for the admin-only FloodWait telemetry command."""

import logging
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.flood_monitor import (
    FloodMonitor,
    flood_origin,
    flood_reason_seconds,
    flood_wait_seconds,
    format_duration,
    is_flood_command,
)


class FloodParsingTests(unittest.TestCase):
    def test_parses_runtime_radar_and_outbox_formats(self):
        self.assertEqual(flood_wait_seconds("Pausado por FloodWait (926s); revisar"), 926)
        self.assertEqual(
            flood_wait_seconds("Radar aguardando FloodWait por 2661s; retomada automática"),
            2661,
        )
        self.assertEqual(
            flood_wait_seconds("Outbox aguardando FloodWait por 31s antes de repetir"),
            31,
        )
        self.assertIsNone(flood_wait_seconds("sem limite"))
        self.assertEqual(flood_reason_seconds("Pausado por FloodWait (10s)"), 10)

    def test_origin_is_classified_without_message_content(self):
        self.assertEqual(
            flood_origin("gr-observer.radar", "Radar aguardando FloodWait por 10s"),
            ("radar", "varredura"),
        )
        self.assertEqual(
            flood_origin("gr-observer.outbox", "Outbox aguardando FloodWait por 10s"),
            ("outbox", "writer"),
        )
        self.assertEqual(
            flood_origin("gr-observer", "Pausado por FloodWait (10s)"),
            ("core", "sessão"),
        )

    def test_command_accepts_bot_mention(self):
        self.assertTrue(is_flood_command("/flood"))
        self.assertTrue(is_flood_command("/flood@RadarGR_ObserverBot"))
        self.assertTrue(is_flood_command("flood"))
        self.assertFalse(is_flood_command("/status"))

    def test_duration_is_human_readable(self):
        self.assertEqual(format_duration(926), "15m 26s")
        self.assertEqual(format_duration(2661), "44m 21s")
        self.assertEqual(format_duration(3661), "1h 01m 01s")


class FloodRenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_render_shows_current_and_history_without_side_effects(self):
        now = datetime.now(timezone.utc)
        pool = NS(
            fetch=AsyncMock(
                side_effect=[
                    [
                        {
                            "module_id": "radar",
                            "reason": "Pausado por FloodWait (926s); revisar antes de ligar",
                            "updated_at": now - timedelta(seconds=30),
                        }
                    ],
                    [
                        {
                            "module_id": "radar",
                            "source": "varredura",
                            "wait_seconds": 2661,
                            "observed_at": now - timedelta(minutes=3),
                        }
                    ],
                ]
            )
        )
        app = NS(pool=pool)
        monitor = FloodMonitor(app)

        text = await monitor.render()

        self.assertIn("🌊 FLOODWAIT", text)
        self.assertIn("15m 26s", text)
        self.assertIn("44m 21s", text)
        self.assertIn("radar/varredura", text)
        self.assertIn("não religa nada", text)
        self.assertEqual(pool.fetch.await_count, 2)

    async def test_log_record_persistence_keeps_only_metadata(self):
        pool = NS(execute=AsyncMock())
        app = NS(pool=pool)
        monitor = FloodMonitor(app)

        await monitor._record("outbox", "writer", 20)

        pool.execute.assert_awaited_once()
        args = pool.execute.await_args.args
        self.assertEqual(args[1:], ("outbox", "writer", 20))


if __name__ == "__main__":
    unittest.main()
