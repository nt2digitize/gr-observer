import inspect
from pathlib import Path
import unittest

from gr_observer.priority_storage import PriorityStorage, SCHEDULER_SCHEMA
from gr_observer.queue_policy import (
    AGING_STEP_SECONDS,
    FRESH_HUMAN_TTL_SECONDS,
    P0_LIVE_HUMAN,
    P1_HUMAN_REACTIVE,
    P2_NORMAL,
    P3_DEFERRED,
    P4_BACKGROUND,
    SimAction,
    base_priority,
    choose_ready_action,
    effective_priority,
    priority_case_sql,
)
from scripts.bench_priority_scheduler import matrix, run_bench


ROOT = Path(__file__).resolve().parent


class PriorityPolicyTests(unittest.TestCase):
    def test_human_waiting_work_precedes_deferred_and_background(self):
        self.assertEqual(base_priority("pv_reply", "send_greeting"), P0_LIVE_HUMAN)
        self.assertEqual(
            base_priority("group_reply", "group_add_contact_reply"),
            P1_HUMAN_REACTIVE,
        )
        self.assertEqual(base_priority("pv_reply", "send_followup"), P3_DEFERRED)
        self.assertEqual(
            base_priority("group_reply", "repost_group_text"), P4_BACKGROUND
        )

    def test_contextual_links_are_p0_only_for_recent_human_origin(self):
        self.assertEqual(
            base_priority(
                "pv_reply",
                "send_live_link",
                action_key="pv_reply:live-link:44:99",
                origin_age_seconds=30,
            ),
            P0_LIVE_HUMAN,
        )
        self.assertEqual(
            base_priority(
                "pv_reply",
                "send_reminder_link",
                action_key="pv_reply:conditional-link:99:weekly:4",
                origin_age_seconds=30,
            ),
            P0_LIVE_HUMAN,
        )
        self.assertEqual(
            base_priority(
                "pv_reply",
                "send_live_link",
                action_key="pv_reply:automated-live-link:44:99",
                origin_age_seconds=30,
            ),
            P2_NORMAL,
        )
        self.assertEqual(
            base_priority(
                "pv_reply",
                "send_reminder_link",
                action_key="pv_reply:conditional-link:99:weekly:4",
                origin_age_seconds=FRESH_HUMAN_TTL_SECONDS + 1,
            ),
            P2_NORMAL,
        )

    def test_contextual_p0_provenance_is_structurally_bound_to_inbound_pv(self):
        storage = (ROOT / "gr_observer" / "storage.py").read_text(encoding="utf-8")
        self.assertEqual(storage.count("pv_reply:live-link:"), 1)
        self.assertEqual(storage.count("pv_reply:conditional-link:"), 1)
        from gr_observer.storage import Storage

        inbound = inspect.getsource(Storage.accept_pv_message)
        self.assertIn("pv_reply:live-link:", inbound)
        self.assertIn("pv_reply:conditional-link:", inbound)

    def test_sql_uses_same_provenance_and_recency_rule(self):
        sql = priority_case_sql("actions")
        self.assertIn("actions.action_key LIKE 'pv_reply:live-link:%'", sql)
        self.assertIn("actions.action_key LIKE 'pv_reply:conditional-link:%'", sql)
        self.assertIn(f"INTERVAL '{FRESH_HUMAN_TTL_SECONDS} seconds'", sql)
        self.assertIn("actions.created_at", sql)

    def test_future_action_never_jumps_the_availability_gate(self):
        future = SimAction("future", "pv_reply", "send_greeting", 61, 1)
        ready = SimAction("ready", "group_reply", "repost_group_text", 0, -1)
        self.assertEqual(
            choose_ready_action([future, ready], now=60, last_lane=None), ready
        )

    def test_same_lane_is_penalized_for_one_turn(self):
        same = SimAction("a", "pv_reply", "send_greeting", 0, 111)
        other = SimAction("b", "pv_reply", "send_greeting", 0, 222)
        chosen = choose_ready_action([same, other], now=0, last_lane="pv:111")
        self.assertEqual(chosen, other)

    def test_background_ages_until_it_can_win(self):
        wait = AGING_STEP_SECONDS * P4_BACKGROUND
        aged = effective_priority(
            "group_reply",
            "repost_group_text",
            wait_seconds=wait,
            lane="group:-10",
            last_lane=None,
        )
        self.assertEqual(aged, P0_LIVE_HUMAN)
        old = SimAction("old", "group_reply", "repost_group_text", 0, -10)
        fresh = SimAction("fresh", "pv_reply", "send_greeting", wait, 999)
        self.assertEqual(
            choose_ready_action([old, fresh], now=wait, last_lane=None), old
        )

    def test_recent_human_link_beats_automated_same_type(self):
        human = SimAction(
            "pv_reply:live-link:1:100",
            "pv_reply",
            "send_live_link",
            100,
            100,
            created_at=90,
        )
        automated = SimAction(
            "pv_reply:automated-live-link:2:200",
            "pv_reply",
            "send_live_link",
            80,
            200,
            created_at=80,
        )
        self.assertEqual(
            choose_ready_action([automated, human], now=100, last_lane=None), human
        )


class PriorityStorageContractTests(unittest.TestCase):
    def test_scheduler_state_is_additive_and_singleton(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS outbox_scheduler_state", SCHEDULER_SCHEMA)
        self.assertIn("PRIMARY KEY DEFAULT TRUE CHECK(singleton)", SCHEDULER_SCHEMA)
        upper = SCHEDULER_SCHEMA.upper()
        self.assertNotIn("DROP ", upper)
        self.assertNotIn("TRUNCATE ", upper)
        self.assertNotIn("DELETE ", upper)

    def test_claim_remains_atomic_and_skip_locked(self):
        source = inspect.getsource(PriorityStorage.claim_next_action)
        self.assertIn("conn.transaction()", source)
        self.assertIn("FOR UPDATE OF actions SKIP LOCKED", source)
        self.assertIn("actions.available_at<=NOW()", source)
        self.assertIn("outbox_scheduler_state", source)
        self.assertNotIn("asyncio.create_task", source)


class SchedulerBenchTests(unittest.TestCase):
    def test_pv_queue_overhead_stays_within_one_writer_slot_under_group_bursts(self):
        for contacts in (1, 5, 10, 20):
            result = run_bench(contacts, prioritized=True)
            self.assertLessEqual(result.pv_queue_overhead, 20.0)

    def test_current_fifo_degrades_but_priority_does_not(self):
        pairs = matrix(include_background=False)
        fifo_waits = [fifo.pv_wait_from_inbound for fifo, _ in pairs]
        priority_waits = [priority.pv_wait_from_inbound for _, priority in pairs]
        self.assertGreater(fifo_waits[-1], fifo_waits[0] + 180)
        self.assertLessEqual(max(priority_waits) - min(priority_waits), 20)

    def test_mixed_old_backlog_does_not_bury_fresh_pv(self):
        for fifo, priority in matrix(include_background=True):
            self.assertLessEqual(priority.pv_queue_overhead, 20.0)
            if fifo.contacts >= 5:
                self.assertLess(priority.pv_wait_from_inbound, fifo.pv_wait_from_inbound)


if __name__ == "__main__":
    unittest.main()
