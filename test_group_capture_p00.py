"""Regression contract for bait capture, P00 scheduling and exact cleanup."""

import inspect
from pathlib import Path
import unittest

from gr_observer.queue_policy import (
    AGING_STEP_SECONDS,
    P00_GROUP_CAPTURE,
    P0_LIVE_HUMAN,
    P1_HUMAN_REACTIVE,
    P4_BACKGROUND,
    SimAction,
    base_priority,
    choose_ready_action,
    effective_priority,
)
from gr_observer.priority_storage import PriorityStorage

ROOT = Path(__file__).resolve().parent


class GroupCapturePriorityTests(unittest.TestCase):
    def test_capture_is_strictly_above_live_pv(self):
        self.assertEqual(
            base_priority("group_reply", "group_capture_contact_reply"),
            P00_GROUP_CAPTURE,
        )
        self.assertLess(P00_GROUP_CAPTURE, P0_LIVE_HUMAN)
        self.assertEqual(
            base_priority("group_reply", "group_add_contact_reply"),
            P1_HUMAN_REACTIVE,
        )
        self.assertEqual(
            base_priority("group_reply", "group_capture_cleanup"),
            P4_BACKGROUND,
        )

    def test_ready_capture_beats_ready_pv_even_after_same_group_lane(self):
        capture = SimAction(
            "group-capture:-10:50:7",
            "group_reply",
            "group_capture_contact_reply",
            0,
            -10,
        )
        pv = SimAction("pv", "pv_reply", "send_greeting", 0, 7)
        chosen = choose_ready_action(
            [pv, capture], now=0, last_lane="group:-10"
        )
        self.assertEqual(chosen, capture)

    def test_future_capture_never_bypasses_available_at(self):
        capture = SimAction(
            "group-capture:-10:50:7",
            "group_reply",
            "group_capture_contact_reply",
            10,
            -10,
        )
        pv = SimAction("pv", "pv_reply", "send_greeting", 0, 7)
        self.assertEqual(
            choose_ready_action([capture, pv], now=0, last_lane=None), pv
        )

    def test_aging_never_manufactures_p00(self):
        aged = effective_priority(
            "group_reply",
            "repost_group_text",
            wait_seconds=AGING_STEP_SECONDS * 100,
            lane="group:-10",
            last_lane=None,
        )
        self.assertEqual(aged, P0_LIVE_HUMAN)
        self.assertNotEqual(aged, P00_GROUP_CAPTURE)

    def test_real_sql_selector_has_explicit_p00_escape(self):
        source = inspect.getsource(PriorityStorage.claim_next_action)
        self.assertIn("P00_GROUP_CAPTURE", source)
        self.assertIn("CASE WHEN", source)
        self.assertIn("actions.available_at<=NOW()", source)
        self.assertIn("FOR UPDATE OF actions SKIP LOCKED", source)


class GroupCaptureStructureTests(unittest.TestCase):
    def setUp(self):
        self.capture = (ROOT / "gr_observer" / "group_capture.py").read_text(
            encoding="utf-8"
        )
        self.flow = (ROOT / "gr_observer" / "group_contact_flow.py").read_text(
            encoding="utf-8"
        )
        self.group = (
            ROOT / "gr_observer" / "modules" / "group_reply_contacts.py"
        ).read_text(encoding="utf-8")
        self.pv = (
            ROOT / "gr_observer" / "modules" / "pv_reply_production.py"
        ).read_text(encoding="utf-8")
        self.env = (ROOT / ".env.example").read_text(encoding="utf-8")

    def test_flag_is_off_by_default(self):
        self.assertIn("GROUP_CAPTURE_P00_ENABLED=false", self.env)
        self.assertIn('_flag("GROUP_CAPTURE_P00_ENABLED", False)', self.flow)

    def test_capture_is_bound_to_current_bait_or_explicit_mention(self):
        self.assertIn("current_bait_id = await self._current_repost_message(chat_id)", self.flow)
        self.assertIn("replied_message_id == current_bait_id", self.flow)
        self.assertIn("direct_bait_reply or mentioned", self.flow)

    def test_capture_mode_replaces_legacy_reactive_product(self):
        self.assertIn("if self.capture_enabled:\n            return False", self.flow)
        observe_pos = self.group.index("await self.contact_flow.observe_event")
        cadence_pos = self.group.index("await self._count_and_queue_repost", observe_pos)
        self.assertLess(observe_pos, cadence_pos)
        self.assertIn("if self.contact_flow.capture_enabled:", self.group)

    def test_dedupe_is_per_bait_user_and_per_source_message(self):
        self.assertIn("PRIMARY KEY(chat_id,bait_message_id,user_id)", self.capture)
        self.assertIn("UNIQUE(chat_id,source_message_id)", self.capture)
        self.assertIn("ON CONFLICT DO NOTHING", self.capture)
        self.assertIn("VALUES($1,$2,$3,'captured',NOW())", self.capture)

    def test_capture_is_immediately_eligible_but_still_outbox_work(self):
        self.assertIn("'group_capture_contact_reply',$3::jsonb,NOW()", self.capture)
        self.assertIn("INSERT INTO outbox_actions", self.capture)
        self.assertNotIn("TelegramClient(", self.capture)
        self.assertNotIn("create_task(", self.capture)

    def test_save_happens_before_group_confirmation(self):
        save_pos = self.flow.index("self.contacts.save_with_effects")
        reply_pos = self.flow.index("SendMessageRequest", save_pos)
        self.assertLess(save_pos, reply_pos)
        self.assertIn("contact_not_saved", self.flow)
        self.assertIn('status="review"', self.flow)
        self.assertIn('status="failed"', self.flow)

    def test_reply_is_exactly_addressed_and_persisted(self):
        self.assertIn("reply_to_msg_id=source_message_id", self.flow)
        self.assertIn('f"@{username} {ADD_REPLY_TEXT}"', self.flow)
        self.assertIn("reply_message_id", self.capture)
        self.assertIn("await self.capture.mark_sent", self.flow)

    def test_cleanup_is_exact_and_never_deletes_human_or_bait(self):
        self.assertIn("CAPTURE_REPLY_TTL_SECONDS = 45 * 60", self.capture)
        self.assertIn("[reply_message_id]", self.flow)
        cleanup_start = self.flow.index("async def action_capture_cleanup")
        cleanup_body = self.flow[cleanup_start:]
        self.assertNotIn("[source_message_id]", cleanup_body)
        self.assertNotIn("[bait_message_id]", cleanup_body)

    def test_pv_arrival_closes_pre_reply_race(self):
        self.assertIn("pv_arrived_at=COALESCE(pv_arrived_at,NOW())", self.capture)
        self.assertIn("if pv_arrived_at is not None", self.capture)
        self.assertIn('"reason": "pv_arrived"', self.capture)
        self.assertIn("queue_private_arrival_cleanup", self.pv)
        self.assertIn('module_id="group_reply"', self.pv)

    def test_human_message_body_is_not_persisted_by_capture_store(self):
        self.assertNotIn("raw_text", self.capture)
        self.assertNotIn("message_text", self.capture)
        self.assertIn('"kind": "group_bait_capture"', self.capture)


if __name__ == "__main__":
    unittest.main()
