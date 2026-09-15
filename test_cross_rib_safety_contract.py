"""Cross-rib regression contract for idempotency, restart and single-writer safety."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class CrossRibSafetyContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = (ROOT / "gr_observer" / "schema.py").read_text(encoding="utf-8")
        self.storage = (ROOT / "gr_observer" / "storage.py").read_text(encoding="utf-8")
        self.priority = (ROOT / "gr_observer" / "priority_storage.py").read_text(encoding="utf-8")
        self.outbox = (ROOT / "gr_observer" / "outbox.py").read_text(encoding="utf-8")
        self.application = (ROOT / "gr_observer" / "application.py").read_text(encoding="utf-8")
        self.contacts = (ROOT / "gr_observer" / "contact_ledger.py").read_text(encoding="utf-8")

    def test_outbox_and_effect_journal_have_hard_idempotency_keys(self):
        self.assertIn("action_key TEXT NOT NULL UNIQUE", self.schema)
        self.assertIn("effect_key TEXT PRIMARY KEY", self.schema)
        self.assertIn("PRIMARY KEY(source, event_key)", self.schema)

    def test_restart_never_blindly_replays_processing_work(self):
        self.assertIn("status='review'", self.storage)
        self.assertIn("WHERE status='processing'", self.storage)
        self.assertNotIn("UPDATE outbox_actions SET status='pending'", self.storage)

    def test_ready_claim_is_atomic_and_never_bypasses_available_at(self):
        self.assertIn("actions.available_at<=NOW()", self.priority)
        self.assertIn("FOR UPDATE OF actions SKIP LOCKED", self.priority)
        self.assertIn("WHERE id=$1 AND status='pending'", self.priority)

    def test_effects_are_journaled_before_external_mutation(self):
        self.assertIn("await self.storage.begin_effect", self.outbox)
        self.assertIn("stable_random_id", self.outbox)
        self.assertIn("await self.storage.finish_effect", self.outbox)
        self.assertIn("AmbiguousExternalEffect", self.outbox)

    def test_production_uses_priority_storage_and_one_user_runtime(self):
        self.assertIn("self.storage = PriorityStorage(self.pool)", self.application)
        self.assertIn("USER_SESSION_LOCK_KEY", self.application)
        self.assertIn("self.writer = None", self.application)
        self.assertNotIn("self.writer2", self.application)

    def test_contact_save_has_durable_per_account_state_and_no_false_success(self):
        self.assertIn("PRIMARY KEY(account_user_id,user_id)", self.schema)
        self.assertIn("save_failed", self.contacts)
        self.assertIn("status='review'", self.contacts)
        self.assertIn("if state in {\"saved\", \"already_saved\"}", self.contacts)

    def test_feature_modules_do_not_create_a_second_user_client_or_writer(self):
        for relative in (
            "gr_observer/modules/pv_reply.py",
            "gr_observer/modules/group_reply.py",
            "gr_observer/modules/radar.py",
            "gr_observer/modules/botson.py",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertNotIn("TelegramClient(", text, relative)
            self.assertNotIn("OutboxWriter(", text, relative)


if __name__ == "__main__":
    unittest.main()
