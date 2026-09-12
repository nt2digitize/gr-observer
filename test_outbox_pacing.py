import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
OUTBOX = (ROOT / "gr_observer" / "outbox.py").read_text(encoding="utf-8")


class OutboxPacingPolicyTests(unittest.TestCase):
    def test_restart_has_warmup_before_backlog(self):
        self.assertIn("OUTBOX_STARTUP_GRACE_SECONDS = 45.0", OUTBOX)
        self.assertIn("if await self._wait_or_stop(self.startup_grace_seconds):", OUTBOX)

    def test_user_actions_are_serially_spaced(self):
        self.assertIn("USER_ACTION_MIN_INTERVAL_SECONDS = 20.0", OUTBOX)
        self.assertIn("action[\"module_id\"] != \"core\"", OUTBOX)
        self.assertIn(
            "self._wait_or_stop(self.user_action_min_interval_seconds)", OUTBOX
        )

    def test_individual_user_writes_are_paced(self):
        self.assertIn("USER_WRITE_MIN_INTERVAL_SECONDS = 3.0", OUTBOX)
        self.assertIn("class UserWritePacer", OUTBOX)
        self.assertIn("before_user_write=self.user_write_pacer.wait", OUTBOX)

    def test_flood_wait_is_obeyed_before_retry(self):
        self.assertIn("except errors.FloodWaitError as exc:", OUTBOX)
        self.assertIn("FLOOD_WAIT_BUFFER_SECONDS = 5", OUTBOX)
        self.assertIn("await asyncio.sleep(wait_seconds)", OUTBOX)


if __name__ == "__main__":
    unittest.main()
