"""Regression contract for the PV Events operator surface."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class PvEventsModuleTests(unittest.TestCase):
    def setUp(self):
        self.integration = (ROOT / "gr_observer" / "group_integration.py").read_text(
            encoding="utf-8"
        )

    def test_events_routes_to_existing_live_blocks(self):
        section = self.integration.split('"events": (', 1)[1].split('"remarketing": (', 1)[0]
        self.assertIn('b"live:new"', section)
        self.assertIn('b"pvm:b:live_optin"', section)
        self.assertIn('b"pvm:b:live_invite"', section)
        self.assertIn('b"pvm:b:live_remarketing"', section)
        self.assertIn('b"pvm:b:live_link"', section)

    def test_events_does_not_add_campaign_runtime(self):
        section = self.integration.split('"events": (', 1)[1].split('"remarketing": (', 1)[0]
        self.assertNotIn("CREATE TABLE", section)
        self.assertNotIn("OutboxWriter(", section)
        self.assertNotIn("TelegramClient(", section)
        self.assertNotIn("create_task(", section)
        self.assertNotIn("INSERT INTO outbox_actions", section)

    def test_current_executable_event_is_explicitly_live(self):
        section = self.integration.split('"events": (', 1)[1].split('"remarketing": (', 1)[0]
        self.assertIn("único tipo executável continua sendo live", section)
        self.assertIn("backend homologado", section)


if __name__ == "__main__":
    unittest.main()
