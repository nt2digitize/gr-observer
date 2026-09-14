import inspect
import unittest

from gr_observer.priority_storage import PriorityStorage, SCHEDULER_SCHEMA
from gr_observer.queue_policy import (
    AGING_STEP_SECONDS,
    P0_LIVE_HUMAN,
    P1_HUMAN_REACTIVE,
    P3_DEFERRED,
    P4_BACKGROUND,
    SimAction,
    base_priority,
    choose_ready_action,
    effective_priority,
)
from scripts.bench_priority_scheduler import matrix, run_bench


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
