import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gr_observer.modules.pv_reply_contacts import PvReplyWithContacts
from gr_observer.modules.pv_reply_production import PvReplyProduction
from gr_observer.pv_temperature import (
    PvTemperatureShadow,
    classify_demand_signal,
    decide_temperature,
)


class DemandSignalTests(unittest.TestCase):
    def test_explicit_link_requests_are_narrow(self):
        for text in (
            "manda o link",
            "me passa o link",
            "cadê o link?",
            "quero o link",
            "qual o link",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_demand_signal(text), "explicit_link_request")

    def test_generic_chat_is_not_promoted(self):
        self.assertEqual(classify_demand_signal("manda aí"), "none")
        self.assertEqual(classify_demand_signal("quero ver"), "none")
        self.assertEqual(classify_demand_signal("boa noite"), "none")
        self.assertEqual(classify_demand_signal("esse link abriu"), "link_mentioned")


class TemperatureDecisionTests(unittest.TestCase):
    def test_recent_explicit_request_with_ready_work_is_hot_vacuum(self):
        decision = decide_temperature(
            demand_signal="explicit_link_request",
            inbound_age_seconds=120,
            recent_group_interaction=False,
            probable_group_origin=False,
            pending_actions=1,
            ready_actions=1,
            stage="following_up",
            suppressed=False,
        )
        self.assertEqual(decision.band, "hot")
        self.assertTrue(decision.vacuum_candidate)
        self.assertGreaterEqual(decision.score, 70)
        self.assertIn("explicit_link_request", decision.reasons)
        self.assertIn("pv_work_ready", decision.reasons)

    def test_group_to_pv_with_pending_work_is_warm_not_forced_hot(self):
        decision = decide_temperature(
            demand_signal="none",
            inbound_age_seconds=300,
            recent_group_interaction=True,
            probable_group_origin=True,
            pending_actions=2,
            ready_actions=0,
            stage="awaiting_reply",
            suppressed=False,
        )
        self.assertEqual(decision.band, "warm")
        self.assertFalse(decision.vacuum_candidate)
        self.assertIn("recent_group_interaction", decision.reasons)

    def test_old_passive_without_work_stays_cold(self):
        decision = decide_temperature(
            demand_signal="none",
            inbound_age_seconds=30 * 3600,
            recent_group_interaction=False,
            probable_group_origin=False,
            pending_actions=0,
            ready_actions=0,
            stage="weekly",
            suppressed=False,
        )
        self.assertEqual(decision.band, "cold")
        self.assertFalse(decision.vacuum_candidate)

    def test_stopped_or_suppressed_is_excluded(self):
        for stage, suppressed in (("stopped", False), ("following_up", True)):
            with self.subTest(stage=stage, suppressed=suppressed):
                decision = decide_temperature(
                    demand_signal="explicit_link_request",
                    inbound_age_seconds=10,
                    recent_group_interaction=True,
                    probable_group_origin=True,
                    pending_actions=3,
                    ready_actions=3,
                    stage=stage,
                    suppressed=suppressed,
                )
                self.assertEqual(decision.band, "excluded")
                self.assertFalse(decision.vacuum_candidate)
                self.assertEqual(decision.score, 0)


class ShadowPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_observation_uses_derived_facts_and_never_needs_raw_text(self):
        pool = SimpleNamespace(
            fetchrow=AsyncMock(
                return_value={
                    "stage": "following_up",
                    "inbound_age_seconds": 100.0,
                    "source_chat_id": -100123,
                    "recent_group_interaction": True,
                    "pending_actions": 1,
                    "ready_actions": 1,
                    "suppressed": False,
                }
            ),
            execute=AsyncMock(),
        )
        shadow = PvTemperatureShadow(pool)
        decision = await shadow.observe(
            event_key="42:99",
            user_id=42,
            demand_signal="explicit_link_request",
        )
        self.assertIsNotNone(decision)
        self.assertTrue(decision.vacuum_candidate)
        args = pool.execute.await_args.args
        self.assertIn("pv_temperature_observations", args[0])
        self.assertEqual(args[3], "explicit_link_request")
        self.assertNotIn("manda o link", repr(args))


class ShadowIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_shadow_failure_never_breaks_primary_pv_event(self):
        storage = SimpleNamespace(pool=SimpleNamespace())
        module = PvReplyProduction(storage, SimpleNamespace())
        module._temperature_shadow_ready = True
        module.temperature_shadow.observe = AsyncMock(side_effect=RuntimeError("shadow only"))
        event = SimpleNamespace(
            is_private=True,
            out=False,
            sender_id=42,
            chat_id=42,
            id=99,
            raw_text="manda o link",
        )
        with patch.object(PvReplyWithContacts, "handle_event", new=AsyncMock(return_value=False)):
            result = await module.handle_event(event)
        self.assertFalse(result)
        module.temperature_shadow.observe.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
