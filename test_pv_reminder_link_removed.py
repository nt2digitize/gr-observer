"""Regression contract: the standalone reminder-link feature is gone."""

import unittest
from pathlib import Path
from types import SimpleNamespace as NS

from gr_observer.pv_message_steps import BLOCK_LABELS, BLOCK_ORDER, _baseline

ROOT = Path(__file__).resolve().parent


class ReminderLinkRemovalTests(unittest.TestCase):
    def test_message_catalog_has_no_reminder_link_block(self):
        self.assertNotIn("reminder_link", BLOCK_ORDER)
        self.assertNotIn("reminder_link", BLOCK_LABELS)
        rows = _baseline(
            NS(
                pv_reply_delay_seconds=60,
                pv_followup_min_hours=23.0,
                pv_followup_max_hours=25.0,
                pv_weekly_interval_hours=168.0,
            )
        )
        self.assertNotIn("reminder_link", {row["block_key"] for row in rows})
        self.assertNotIn("reminder.link", {row["step_key"] for row in rows})

    def test_runtime_no_longer_contains_reminder_link_feature(self):
        paths = (
            ROOT / "gr_observer" / "modules" / "pv_reply.py",
            ROOT / "gr_observer" / "pv_message_runtime.py",
            ROOT / "gr_observer" / "storage.py",
            ROOT / "gr_observer" / "pv_message_panel.py",
        )
        for path in paths:
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("send_reminder_link", source, str(path))
            self.assertNotIn("conditional-link", source, str(path))
            self.assertNotIn('"reminder_link"', source, str(path))


if __name__ == "__main__":
    unittest.main()
