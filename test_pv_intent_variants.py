import inspect
import unittest

from gr_observer.pv_intent_variants import (
    DDL,
    PvIntentVariantStore,
    VARIANT_SLOTS,
    next_variant_slot,
)


class FakeVariantDb:
    def __init__(self):
        self.variants = []
        self.history = []
        self._variant_id = 0
        self._history_id = 0

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query, *args):
        return "OK"

    async def fetch(self, query, *args):
        if "FROM pv_linear_intent_variants" in query:
            step_id, intent_key = int(args[0]), str(args[1])
            rows = [
                row
                for row in self.variants
                if row["step_id"] == step_id
                and row["intent_key"] == intent_key
                and row["enabled"]
            ]
            return sorted(rows, key=lambda row: (VARIANT_SLOTS.index(row["slot"]), row["id"]))
        raise AssertionError(f"fetch inesperado: {query}")

    def _history_join(self, action_key):
        item = next((row for row in self.history if row["action_key"] == action_key), None)
        if item is None:
            return None
        variant = next(row for row in self.variants if row["id"] == item["variant_id"])
        return {**item, "content": variant["content"]}

    async def fetchrow(self, query, *args):
        if "FROM pv_linear_variant_history h" in query and "WHERE h.action_key=$1" in query:
            return self._history_join(str(args[0]))
        if "INSERT INTO pv_linear_variant_history" in query:
            action_key = str(args[0])
            if self._history_join(action_key) is not None:
                return None
            self._history_id += 1
            row = {
                "id": self._history_id,
                "action_key": action_key,
                "user_id": int(args[1]),
                "step_id": int(args[2]),
                "intent_key": str(args[3]),
                "slot": str(args[4]),
                "variant_id": int(args[5]),
            }
            self.history.append(row)
            return row
        raise AssertionError(f"fetchrow inesperado: {query}")

    async def fetchval(self, query, *args):
        if "SELECT slot FROM pv_linear_variant_history" in query:
            user_id, intent_key = int(args[0]), str(args[1])
            rows = [
                row
                for row in self.history
                if row["user_id"] == user_id and row["intent_key"] == intent_key
            ]
            return rows[-1]["slot"] if rows else None
        if "INSERT INTO pv_linear_intent_variants" in query:
            step_id, intent_key, slot, content = (
                int(args[0]),
                str(args[1]),
                str(args[2]),
                str(args[3]),
            )
            existing = next(
                (
                    row
                    for row in self.variants
                    if row["step_id"] == step_id
                    and row["intent_key"] == intent_key
                    and row["slot"] == slot
                ),
                None,
            )
            if existing is not None:
                existing["content"] = content
                existing["enabled"] = True
                return existing["id"]
            self._variant_id += 1
            self.variants.append(
                {
                    "id": self._variant_id,
                    "step_id": step_id,
                    "intent_key": intent_key,
                    "slot": slot,
                    "content": content,
                    "enabled": True,
                }
            )
            return self._variant_id
        raise AssertionError(f"fetchval inesperado: {query}")


async def configure(store, step_id=10, intent="pre_join", slots=VARIANT_SLOTS):
    for slot in slots:
        await store.upsert_variant(
            step_id=step_id,
            intent_key=intent,
            slot=slot,
            content=f"fala-{intent}-{slot}",
        )


class VariantMathTests(unittest.TestCase):
    def test_full_rotation_is_a_b_c_d(self):
        self.assertEqual(next_variant_slot(VARIANT_SLOTS, None), "A")
        self.assertEqual(next_variant_slot(VARIANT_SLOTS, "A"), "B")
        self.assertEqual(next_variant_slot(VARIANT_SLOTS, "B"), "C")
        self.assertEqual(next_variant_slot(VARIANT_SLOTS, "C"), "D")
        self.assertEqual(next_variant_slot(VARIANT_SLOTS, "D"), "A")

    def test_missing_slot_is_skipped_without_immediate_repeat(self):
        self.assertEqual(next_variant_slot(("A", "C"), "A"), "C")
        self.assertEqual(next_variant_slot(("A", "C"), "C"), "A")


class VariantPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_lead_rotates_a_b_c_d_and_keeps_history(self):
        db = FakeVariantDb()
        store = PvIntentVariantStore(db)
        await configure(store)

        slots = []
        for index in range(1, 6):
            selected = await store.select_for_action(
                action_key=f"linear:user1:{index}",
                user_id=1,
                step_id=10,
                intent_keys=("pre_join",),
            )
            slots.append(selected.slot)

        self.assertEqual(slots, ["A", "B", "C", "D", "A"])
        self.assertEqual(len(db.history), 5)

    async def test_retry_of_same_action_keeps_same_variant_and_one_history_row(self):
        db = FakeVariantDb()
        store = PvIntentVariantStore(db)
        await configure(store)

        first = await store.select_for_action(
            action_key="linear:user1:retry",
            user_id=1,
            step_id=10,
            intent_keys=("pre_join",),
        )
        retry = await store.select_for_action(
            action_key="linear:user1:retry",
            user_id=1,
            step_id=10,
            intent_keys=("pre_join",),
        )

        self.assertEqual(first.slot, "A")
        self.assertEqual(retry.slot, "A")
        self.assertEqual(first.variant_id, retry.variant_id)
        self.assertEqual(len(db.history), 1)

    async def test_each_lead_has_independent_rotation(self):
        db = FakeVariantDb()
        store = PvIntentVariantStore(db)
        await configure(store)

        u1a = await store.select_for_action(
            action_key="u1:a", user_id=1, step_id=10, intent_keys=("pre_join",)
        )
        u1b = await store.select_for_action(
            action_key="u1:b", user_id=1, step_id=10, intent_keys=("pre_join",)
        )
        u2a = await store.select_for_action(
            action_key="u2:a", user_id=2, step_id=10, intent_keys=("pre_join",)
        )

        self.assertEqual((u1a.slot, u1b.slot, u2a.slot), ("A", "B", "A"))

    async def test_first_allowed_intent_with_configured_copy_wins(self):
        db = FakeVariantDb()
        store = PvIntentVariantStore(db)
        await configure(store, intent="joined", slots=("A", "B"))

        selected = await store.select_for_action(
            action_key="u1:joined",
            user_id=1,
            step_id=10,
            intent_keys=("pre_join", "joined", "post_join"),
        )

        self.assertEqual(selected.intent_key, "joined")
        self.assertEqual(selected.slot, "A")
        self.assertEqual(selected.content, "fala-joined-A")

    async def test_no_configured_variant_returns_none_without_history(self):
        db = FakeVariantDb()
        store = PvIntentVariantStore(db)
        selected = await store.select_for_action(
            action_key="u1:none",
            user_id=1,
            step_id=10,
            intent_keys=("pre_join",),
        )
        self.assertIsNone(selected)
        self.assertFalse(db.history)


class VariantSafetyContractTests(unittest.TestCase):
    def test_schema_is_additive_and_action_key_is_retry_guard(self):
        upper = DDL.upper()
        self.assertIn("ACTION_KEY TEXT NOT NULL UNIQUE", upper)
        self.assertIn("PV_LINEAR_VARIANT_HISTORY", upper)
        self.assertNotIn("DROP ", upper)
        self.assertNotIn("TRUNCATE ", upper)
        self.assertNotIn("DELETE FROM", upper)

    def test_selector_has_no_telegram_or_progression_surface(self):
        source = inspect.getsource(PvIntentVariantStore.select_for_action).casefold()
        for token in (
            "telegramclient",
            "send_message",
            "send_file",
            "outbox_actions",
            "next_step",
            "pv_linear_sessions",
        ):
            self.assertNotIn(token, source)

    def test_selector_history_is_per_user_and_intent(self):
        source = inspect.getsource(PvIntentVariantStore.select_for_action)
        self.assertIn("WHERE user_id=$1 AND intent_key=$2", source)
        self.assertIn("ON CONFLICT(action_key) DO NOTHING", source)


if __name__ == "__main__":
    unittest.main()
