import inspect
import unittest
from decimal import Decimal

from gr_observer.pv_linear_capabilities import (
    CAPABILITY_MIGRATION_VERSION,
    LIVE_OPTIN_STEP_KEY,
    TWO_SCREENS_CHOICE_STEP_KEY,
    TWO_SCREENS_PROMPT_STEP_KEY,
    PvLinearCapabilityStore,
)


class FakeCapabilityDb:
    def __init__(self):
        self.migrations = set()
        self.steps = {
            "link.preview": {
                "id": 1,
                "linear_position": Decimal("2000000"),
                "linear_enabled": True,
                "wait_for_reply": False,
                "phase_key": "preview",
            },
            "followup.text": {
                "id": 2,
                "linear_position": Decimal("3000000"),
                "linear_enabled": True,
                "wait_for_reply": False,
                "phase_key": "warmup",
            },
            TWO_SCREENS_PROMPT_STEP_KEY: {
                "id": 3,
                "linear_position": None,
                "linear_enabled": False,
                "wait_for_reply": False,
                "phase_key": None,
            },
            TWO_SCREENS_CHOICE_STEP_KEY: {
                "id": 4,
                "linear_position": None,
                "linear_enabled": False,
                "wait_for_reply": False,
                "phase_key": None,
            },
            LIVE_OPTIN_STEP_KEY: {
                "id": 5,
                "linear_position": None,
                "linear_enabled": False,
                "wait_for_reply": False,
                "phase_key": None,
            },
        }
        self.contacts = set()
        self.two = {}
        self.subscriptions = {}
        self.live = {}
        self.outbox = {}
        self.linear_sessions = {}

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetchval(self, query, *args):
        if "pv_message_step_migrations WHERE version" in query:
            return 1 if int(args[0]) in self.migrations else None
        if "step_key='link.preview'" in query:
            return self.steps["link.preview"]["linear_position"]
        if "linear_enabled IS TRUE AND linear_position>$1" in query:
            current = Decimal(args[0])
            positions = [
                row["linear_position"]
                for row in self.steps.values()
                if row["linear_enabled"] and row["linear_position"] is not None
                and row["linear_position"] > current
            ]
            return min(positions) if positions else None
        if "UPDATE pv_message_steps" in query and "RETURNING id" in query:
            step_key, position, wait = str(args[0]), Decimal(args[1]), bool(args[2])
            row = self.steps.get(step_key)
            if row is None:
                return None
            row.update(
                linear_position=position,
                linear_enabled=True,
                wait_for_reply=wait,
                phase_key="warmup",
            )
            return row["id"]
        if "SELECT status FROM pv_two_screens_sessions" in query:
            return self.two.get(int(args[0]))
        if "UPDATE live_alert_subscriptions" in query and "RETURNING user_id" in query:
            user_id, status = int(args[0]), str(args[1])
            if self.subscriptions.get(user_id) != "pending":
                return None
            self.subscriptions[user_id] = status
            return user_id
        raise AssertionError(f"fetchval inesperado: {query}")

    async def fetchrow(self, query, *args):
        if "FROM pv_linear_sessions s" in query:
            user_id = int(args[0])
            session = self.linear_sessions.get(user_id)
            if session is None:
                return None
            step = next(
                (row for row in self.steps.values() if row["id"] == session["current_step_id"]),
                None,
            )
            return {**session, "step_key": None if step is None else next(
                key for key, value in self.steps.items() if value is step
            )}
        if "FROM live_campaign_recipients" in query:
            user_id = int(args[0])
            rows = [
                {"campaign_id": cid, "stage": stage}
                for (cid, uid), stage in self.live.items()
                if uid == user_id and stage in {"awaiting", "remarketing_sent"}
            ]
            return sorted(rows, key=lambda row: row["campaign_id"], reverse=True)[0] if rows else None
        raise AssertionError(f"fetchrow inesperado: {query}")

    async def execute(self, query, *args):
        if "INSERT INTO pv_message_step_migrations" in query:
            self.migrations.add(int(args[0]))
            return "OK"
        if "INSERT INTO pv_reply_contacts" in query:
            self.contacts.add(int(args[0]))
            return "OK"
        if "INSERT INTO pv_two_screens_sessions" in query:
            self.two[int(args[0])] = "prompt_queued"
            return "OK"
        if "UPDATE pv_two_screens_sessions" in query and "status='completed'" in query:
            user_id = int(args[0])
            if self.two.get(user_id) not in {"completed", "stopped"}:
                self.two[user_id] = "completed"
            return "OK"
        if "UPDATE live_campaign_recipients" in query:
            campaign_id, user_id = int(args[0]), int(args[1])
            self.live[(campaign_id, user_id)] = (
                "stopped" if "stage='stopped'" in query else "link_queued"
            )
            return "OK"
        if "INSERT INTO outbox_actions" in query:
            key = str(args[0])
            self.outbox.setdefault(key, args[1])
            return "OK"
        return "OK"


class CapabilityMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_capabilities_are_inserted_after_preview_in_order(self):
        db = FakeCapabilityDb()
        store = PvLinearCapabilityStore(db)
        await store.ensure_ready()

        positions = [
            db.steps[key]["linear_position"]
            for key in (
                "link.preview",
                TWO_SCREENS_PROMPT_STEP_KEY,
                TWO_SCREENS_CHOICE_STEP_KEY,
                LIVE_OPTIN_STEP_KEY,
                "followup.text",
            )
        ]
        self.assertEqual(positions, sorted(positions))
        self.assertFalse(db.steps[TWO_SCREENS_PROMPT_STEP_KEY]["wait_for_reply"])
        self.assertTrue(db.steps[TWO_SCREENS_CHOICE_STEP_KEY]["wait_for_reply"])
        self.assertTrue(db.steps[LIVE_OPTIN_STEP_KEY]["wait_for_reply"])
        self.assertIn(CAPABILITY_MIGRATION_VERSION, db.migrations)

    async def test_waiting_on_matches_only_current_waiting_capability(self):
        db = FakeCapabilityDb()
        store = PvLinearCapabilityStore(db)
        await store.ensure_ready()
        db.linear_sessions[42] = {
            "user_id": 42,
            "status": "waiting_reply",
            "current_step_id": db.steps[TWO_SCREENS_CHOICE_STEP_KEY]["id"],
            "current_position": db.steps[TWO_SCREENS_CHOICE_STEP_KEY]["linear_position"],
            "generation": 1,
        }
        self.assertTrue(await store.waiting_on(42, TWO_SCREENS_CHOICE_STEP_KEY))
        self.assertFalse(await store.waiting_on(42, LIVE_OPTIN_STEP_KEY))


class TwoScreensStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_linear_lead_gets_inert_legacy_contact_shell_and_session(self):
        db = FakeCapabilityDb()
        store = PvLinearCapabilityStore(db)
        self.assertEqual(await store.prepare_two_screens(42), "ready")
        self.assertIn(42, db.contacts)
        self.assertEqual(db.two[42], "prompt_queued")

    async def test_completed_or_active_legacy_session_is_not_reopened(self):
        db = FakeCapabilityDb()
        store = PvLinearCapabilityStore(db)
        db.two[42] = "completed"
        self.assertEqual(await store.prepare_two_screens(42), "already_done")
        db.two[42] = "awaiting_choice"
        self.assertEqual(await store.prepare_two_screens(42), "legacy_active")

    async def test_complete_two_screens_closes_capability_without_deleting_history(self):
        db = FakeCapabilityDb()
        db.two[42] = "awaiting_choice"
        store = PvLinearCapabilityStore(db)
        await store.complete_two_screens(42)
        self.assertEqual(db.two[42], "completed")


class LiveCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_optin_response_changes_only_pending_subscription(self):
        db = FakeCapabilityDb()
        db.subscriptions[42] = "pending"
        store = PvLinearCapabilityStore(db)
        self.assertEqual(await store.handle_live_optin_response(42, "positive"), "subscribed")
        self.assertEqual(db.subscriptions[42], "subscribed")
        self.assertEqual(await store.handle_live_optin_response(42, "negative"), "not_pending")

    async def test_live_positive_queues_existing_link_action_and_negative_stops(self):
        db = FakeCapabilityDb()
        store = PvLinearCapabilityStore(db)
        db.live[(7, 42)] = "awaiting"
        outcome = await store.handle_active_live_response(42, "positive")
        self.assertEqual(outcome, "live_link_queued")
        self.assertEqual(db.live[(7, 42)], "link_queued")
        self.assertIn("pv_reply:live-link:7:42", db.outbox)

        db.live[(8, 42)] = "remarketing_sent"
        outcome = await store.handle_active_live_response(42, "negative")
        self.assertEqual(outcome, "live_declined")
        self.assertEqual(db.live[(8, 42)], "stopped")


class CapabilitySafetyContractTests(unittest.TestCase):
    def test_bridge_reuses_existing_state_and_has_no_direct_telegram_surface(self):
        source = inspect.getsource(PvLinearCapabilityStore)
        for token in (
            "TelegramClient(",
            "SafeOutboxWriter(",
            "OutboxWriter(",
            "send_message(",
            "send_file(",
            "create_task(",
            "DROP TABLE",
            "TRUNCATE",
            "DELETE FROM",
        ):
            self.assertNotIn(token, source)
        self.assertIn("pv_two_screens_sessions", source)
        self.assertIn("live_alert_subscriptions", source)
        self.assertIn("live_campaign_recipients", source)
        self.assertIn("outbox_actions", source)


if __name__ == "__main__":
    unittest.main()
