"""Regression contract for the lightweight PV remarketing menu."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class PvRemarketingMenuTests(unittest.TestCase):
    def test_menu_routes_only_to_existing_blocks(self):
        source = (ROOT / "gr_observer" / "group_integration.py").read_text(encoding="utf-8")
        section = source.split('"remarketing": (', 1)[1].split('"conversation": (', 1)[0]
        for callback in (
            'b"pvm:b:followup"',
            'b"pvm:b:reminder_link"',
            'b"pvm:b:weekly"',
            'b"pvm:b:live_remarketing"',
        ):
            self.assertIn(callback, section)

    def test_menu_does_not_invent_monthly_scheduler_or_runtime(self):
        source = (ROOT / "gr_observer" / "group_integration.py").read_text(encoding="utf-8")
        section = source.split('"remarketing": (', 1)[1].split('"conversation": (', 1)[0]
        self.assertNotIn("monthly", section.lower())
        self.assertNotIn("OutboxWriter(", section)
        self.assertNotIn("TelegramClient(", section)
        self.assertNotIn("create_task(", section)

    def test_existing_runtime_action_types_are_untouched(self):
        runtime = (ROOT / "gr_observer" / "pv_message_runtime.py").read_text(encoding="utf-8")
        self.assertIn('block_key="followup"', runtime)
        self.assertIn('block_key="weekly"', runtime)
        self.assertIn('block_key="live_remarketing"', runtime)


if __name__ == "__main__":
    unittest.main()
