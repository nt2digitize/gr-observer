"""Regression contract for the PV Entry operator module."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class PvEntryModuleTests(unittest.TestCase):
    def setUp(self):
        self.integration = (ROOT / "gr_observer" / "group_integration.py").read_text(
            encoding="utf-8"
        )

    def test_entry_routes_to_existing_blocks_only(self):
        self.assertIn('Button.inline("👋 Saudação", b"pvm:b:greeting")', self.integration)
        self.assertIn('Button.inline("🔗 Primeiro acesso / link", b"pvm:b:link")', self.integration)
        self.assertIn('Button.inline("💬 Pós-link", b"pvm:b:followup")', self.integration)

    def test_entry_adds_no_runtime_or_queue_primitive(self):
        entry = self.integration.split('"entry": (', 1)[1].split('"two_screens": (', 1)[0]
        self.assertNotIn("TelegramClient(", entry)
        self.assertNotIn("OutboxWriter(", entry)
        self.assertNotIn("create_task(", entry)
        self.assertNotIn("INSERT INTO outbox_actions", entry)

    def test_entry_keeps_existing_editor_callbacks(self):
        entry = self.integration.split('"entry": (', 1)[1].split('"two_screens": (', 1)[0]
        self.assertIn('b"pvm:b:greeting"', entry)
        self.assertIn('b"pvm:b:link"', entry)
        self.assertIn('b"pvm:b:followup"', entry)
        self.assertNotIn("send_greeting", entry)
        self.assertNotIn("send_link", entry)
        self.assertNotIn("send_followup", entry)


if __name__ == "__main__":
    unittest.main()
