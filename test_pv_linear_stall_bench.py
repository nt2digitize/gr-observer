import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gr_observer.pv_linear_capabilities import LIVE_OPTIN_STEP_KEY
from gr_observer.pv_linear_capability_runtime import PvLinearCapabilityRuntimeMixin
from gr_observer.pv_linear_flow import PvLinearRuntimeMixin


PEER = 8795995074


class FakeSender:
    def __init__(self, user_id=PEER):
        self.id = user_id
        self.bot = False
        self.deleted = False
        self.support = False
        self.username = "ENGNJ"
        self.first_name = "ENGNJ"
        self.last_name = None


class FakeEvent:
    is_private = True
    out = False

    def __init__(self, text, message_id, user_id=PEER):
        self.raw_text = text
        self.id = message_id
        self.sender_id = user_id
        self.chat_id = user_id
        self._sender = FakeSender(user_id)

    async def get_sender(self):
        return self._sender


class FakePool:
    def __init__(self, session):
        self.session = session

    async def fetchrow(self, query, *args):
        if "FROM pv_linear_sessions WHERE user_id=$1" in query:
            return dict(self.session)
        raise AssertionError(f"fetchrow inesperado: {query}")

    async def fetchval(self, query, *args):
        if "UPDATE pv_linear_sessions" in query and "RETURNING generation" in query:
            peer, step_id, position, expected_step = args
            if int(peer) != PEER:
                return None
            if self.session["status"] != "waiting_reply":
                return None
            if int(self.session["current_step_id"]) != int(expected_step):
                return None
            self.session.update(
                status="active",
                current_step_id=int(step_id),
                current_position=position,
            )
            return int(self.session["generation"])
        raise AssertionError(f"fetchval inesperado: {query}")

    async def execute(self, query, *args):
        if "last_inbound_message_id=$2" in query:
            self.session["last_inbound_message_id"] = int(args[1])
            return "OK"
        if "SET status='active',current_step_id=$2,current_position=$3" in query:
            self.session.update(
                status="active",
                current_step_id=int(args[1]),
                current_position=args[2],
            )
            return "OK"
        if "SET status='completed'" in query:
            self.session["status"] = "completed"
            return "OK"
        raise AssertionError(f"execute inesperado: {query}")


class FakeLinearStore:
    def __init__(self, rows):
        self.rows = list(rows)

    async def ensure_ready(self):
        return None

    async def next_step(self, after_position):
        for row in self.rows:
            if row["linear_position"] > after_position:
                return row
        return None


class FakeCapabilityStore:
    def __init__(self, session, step_keys):
        self.session = session
        self.step_keys = step_keys
        self.subscription = "pending"

    async def waiting_step(self, user_id):
        return {
            **self.session,
            "step_key": self.step_keys.get(int(self.session["current_step_id"])),
        }

    async def handle_live_optin_response(self, user_id, response_kind):
        if response_kind not in {"positive", "negative"}:
            return "waiting"
        if self.subscription != "pending":
            return "not_pending"
        self.subscription = "subscribed" if response_kind == "positive" else "declined"
        return self.subscription

    async def handle_active_live_response(self, user_id, response_kind):
        return None


class BenchRuntime(PvLinearCapabilityRuntimeMixin, PvLinearRuntimeMixin):
    def __init__(self, session, rows, step_keys):
        self.storage = SimpleNamespace(pool=FakePool(session))
        self.linear_store = FakeLinearStore(rows)
        self.linear_capability_store = FakeCapabilityStore(session, step_keys)
        self._linear_capability_ready = True
        self.me = None
        self.auto_save_contacts = False
        self.queued = []

    async def _queue_linear_step(self, peer, row, generation):
        self.queued.append((int(peer), int(row["id"]), int(generation)))
        return True


class LinearStallBenchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "PV_LINEAR_FLOW_ENABLED": "true",
                "PV_LINEAR_TEST_USER_IDS": str(PEER),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    async def test_live_sim_resumes_same_generation_and_queues_exact_next_step(self):
        session = {
            "user_id": PEER,
            "status": "waiting_reply",
            "current_step_id": 10,
            "current_position": 30,
            "generation": 2,
            "last_inbound_message_id": 100,
        }
        rows = [
            {"id": 11, "linear_position": 40},
            {"id": 12, "linear_position": 50},
        ]
        runtime = BenchRuntime(session, rows, {10: LIVE_OPTIN_STEP_KEY, 11: "after.live", 12: "next"})

        await runtime.handle_event(FakeEvent("sim", 101))

        self.assertEqual(runtime.linear_capability_store.subscription, "subscribed")
        self.assertEqual(session["status"], "active")
        self.assertEqual(session["current_step_id"], 11)
        self.assertEqual(session["generation"], 2)
        self.assertEqual(runtime.queued, [(PEER, 11, 2)])

    async def test_generic_reply_advances_only_when_lane_is_waiting(self):
        session = {
            "user_id": PEER,
            "status": "waiting_reply",
            "current_step_id": 11,
            "current_position": 40,
            "generation": 2,
            "last_inbound_message_id": 101,
        }
        rows = [{"id": 12, "linear_position": 50}]
        runtime = BenchRuntime(session, rows, {11: "after.live", 12: "next"})

        with patch("gr_observer.pv_linear_flow.is_suppressed", new=AsyncMock(return_value=False)):
            await runtime.handle_event(FakeEvent("kkk", 102))

        self.assertEqual(session["last_inbound_message_id"], 102)
        self.assertEqual(session["status"], "active")
        self.assertEqual(session["current_step_id"], 12)
        self.assertEqual(runtime.queued, [(PEER, 12, 2)])

    async def test_generic_reply_does_not_skip_an_already_active_lane(self):
        session = {
            "user_id": PEER,
            "status": "active",
            "current_step_id": 11,
            "current_position": 40,
            "generation": 2,
            "last_inbound_message_id": 101,
        }
        rows = [{"id": 12, "linear_position": 50}]
        runtime = BenchRuntime(session, rows, {11: "after.live", 12: "next"})

        with patch("gr_observer.pv_linear_flow.is_suppressed", new=AsyncMock(return_value=False)):
            await runtime.handle_event(FakeEvent("kkk", 102))

        self.assertEqual(session["last_inbound_message_id"], 102)
        self.assertEqual(session["status"], "active")
        self.assertEqual(session["current_step_id"], 11)
        self.assertEqual(runtime.queued, [])


if __name__ == "__main__":
    unittest.main()
