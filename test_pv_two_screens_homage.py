"""Regression contract for two-screens homage capture."""

import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.pv_homage import PvHomageStore

ROOT = Path(__file__).resolve().parent


class PvTwoScreensHomageTests(unittest.IsolatedAsyncioTestCase):
    async def test_homage_requires_a_recent_succeeded_two_screens_photo(self):
        pool = NS(fetchval=AsyncMock(return_value=True))
        store = PvHomageStore(pool)
        self.assertTrue(await store.can_accept(123))
        sql = pool.fetchval.await_args.args[0]
        self.assertIn("send_two_screens_photo", sql)
        self.assertIn("a.status='succeeded'", sql)
        self.assertIn("awaiting_choice", sql)
        self.assertIn("completed", sql)
        self.assertIn("INTERVAL '24 hours'", sql)

    def test_homage_persists_metadata_only_in_existing_inbox(self):
        source = (ROOT / "gr_observer" / "pv_homage.py").read_text(encoding="utf-8")
        self.assertIn("telegram-user-homage", source)
        self.assertIn("two_screens_homage", source)
        self.assertIn("inbox_events", source)
        self.assertNotIn("CREATE TABLE", source)
        self.assertNotIn("BYTEA", source)
        self.assertNotIn("download_media", source)
        self.assertNotIn("upload_file", source)

    def test_media_homage_is_intercepted_before_legacy_choice_flow(self):
        source = (ROOT / "gr_observer" / "modules" / "pv_reply_production.py").read_text(
            encoding="utf-8"
        )
        handle = source.split("async def handle_event", 1)[1].split("async def _send_row", 1)[0]
        self.assertLess(handle.index("_capture_two_screens_homage"), handle.index("super().handle_event"))
        for kind in ('return "photo"', 'return "video"', 'return "gif"'):
            self.assertIn(kind, source)

    def test_homage_reply_reuses_existing_generic_message_step(self):
        source = (ROOT / "gr_observer" / "modules" / "pv_reply_production.py").read_text(
            encoding="utf-8"
        )
        capture = source.split("async def _capture_two_screens_homage", 1)[1].split(
            "async def handle_event", 1
        )[0]
        self.assertIn('_next_block_wait("two_screens_followup"', capture)
        self.assertIn("_queue_sequence_continuation(", capture)
        self.assertNotIn("send_two_screens_homage", capture)

    def test_panel_groups_existing_two_screens_controls(self):
        integration = (ROOT / "gr_observer" / "group_integration.py").read_text(
            encoding="utf-8"
        )
        section = integration.split('"two_screens": (', 1)[1].split('"events": (', 1)[0]
        self.assertIn('b"pvm:b:two_screens_prompt"', section)
        self.assertIn('b"pvm:b:two_screens_preference"', section)
        self.assertIn('b"pv:two_screens"', section)
        self.assertIn('b"pvm:b:two_screens_followup"', section)
        self.assertIn('b"pvm:b:two_screens_retry_preference"', section)
        self.assertNotIn("OutboxWriter(", section)
        self.assertNotIn("TelegramClient(", section)


if __name__ == "__main__":
    unittest.main()
