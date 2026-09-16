"""Regression tests for the operator-visible per-user PV pause control."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class PvPauseMenuTests(unittest.TestCase):
    def setUp(self):
        self.integration = (ROOT / "gr_observer" / "group_integration.py").read_text(
            encoding="utf-8"
        )
        self.suppression = (ROOT / "gr_observer" / "pv_suppression.py").read_text(
            encoding="utf-8"
        )
        self.priority = (ROOT / "gr_observer" / "priority_storage.py").read_text(
            encoding="utf-8"
        )

    def test_daily_pv_menu_exposes_per_user_pause(self):
        self.assertIn('Button.inline("⏸ Pausar chat de uma pessoa", b"pvstop:picker")', self.integration)
        self.assertIn('data == "pvstop:picker"', self.integration)
        self.assertIn('b"menu:pv"', self.integration)
        self.assertIn("Nenhum outro chat é afetado", self.integration)

    def test_pause_is_application_only_not_telegram_block(self):
        self.assertIn("não bloqueia no Telegram", self.integration)
        self.assertNotIn("BlockRequest", self.integration)
        self.assertNotIn("DeleteContactsRequest", self.integration)
        self.assertNotIn("BlockRequest", self.suppression)
        self.assertNotIn("DeleteContactsRequest", self.suppression)

    def test_pause_neutralizes_pending_pv_work_and_stops_journey(self):
        self.assertIn("UPDATE pv_reply_contacts SET stage='stopped'", self.suppression)
        self.assertIn("module_id='pv_reply' AND status='pending'", self.suppression)
        self.assertIn("admin_suppressed", self.suppression)

    def test_scheduler_refuses_new_or_pending_work_for_suppressed_user(self):
        self.assertIn("if await is_suppressed(self.pool, user_id)", self.priority)
        self.assertIn("pv_suppressed_users", self.priority)
        self.assertIn("admin_suppressed", self.priority)

    def test_pause_adds_no_client_writer_or_worker(self):
        self.assertNotIn("TelegramClient(", self.integration)
        self.assertNotIn("OutboxWriter(", self.integration)
        self.assertNotIn("create_task(", self.integration)


if __name__ == "__main__":
    unittest.main()
