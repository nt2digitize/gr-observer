import inspect
import unittest
from types import SimpleNamespace

from telethon import types

from gr_observer.telegram_peers import (
    DurablePeerStore,
    DurableTelegramClient,
    PeerReferenceUnavailable,
)


class FakePool:
    def __init__(self):
        self.rows = {}
        self.executed = []

    async def execute(self, query, *args):
        self.executed.append((query, args))
        if "INSERT INTO telegram_peer_refs" in query and len(args) == 3:
            self.rows[(int(args[0]), int(args[1]))] = int(args[2])
        return "OK"

    async def fetchrow(self, query, *args):
        if "SELECT access_hash FROM telegram_peer_refs" in query:
            value = self.rows.get((int(args[0]), int(args[1])))
            return None if value is None else {"access_hash": value}
        return None


class DurablePeerTests(unittest.IsolatedAsyncioTestCase):
    async def test_numeric_peer_resolves_from_account_scoped_reference(self):
        pool = FakePool()
        pool.rows[(999, 42)] = 123456789
        store = DurablePeerStore(pool)
        store.account_user_id = 999
        resolved = await store.resolve_user(42)
        self.assertIsInstance(resolved, types.InputPeerUser)
        self.assertEqual(resolved.user_id, 42)
        self.assertEqual(resolved.access_hash, 123456789)

    async def test_peer_reference_is_scoped_to_operator_account(self):
        pool = FakePool()
        pool.rows[(111, 42)] = 123456789
        store = DurablePeerStore(pool)
        store.account_user_id = 999
        self.assertIsNone(await store.resolve_user(42))

    async def test_inbound_private_input_sender_is_persisted_and_wakes_peer(self):
        pool = FakePool()
        store = DurablePeerStore(pool)
        store.account_user_id = 999
        event = SimpleNamespace(
            out=False,
            is_private=True,
            input_sender=types.InputPeerUser(user_id=42, access_hash=987654321),
        )
        await store.remember_event(event)
        self.assertEqual(pool.rows[(999, 42)], 987654321)
        sql = "\n".join(query for query, _ in pool.executed)
        self.assertIn("payload->>'peer'=$1", sql)

    async def test_zero_hash_is_not_persisted(self):
        pool = FakePool()
        store = DurablePeerStore(pool)
        store.account_user_id = 999
        event = SimpleNamespace(
            out=False,
            is_private=True,
            input_sender=types.InputPeerUser(user_id=42, access_hash=0),
        )
        await store.remember_event(event)
        self.assertNotIn((999, 42), pool.rows)

    def test_client_fallback_is_static_class_behavior_not_monkey_patch(self):
        source = inspect.getsource(DurableTelegramClient.get_input_entity)
        self.assertIn("super().get_input_entity", source)
        self.assertIn("resolve_user", source)
        self.assertIn("PeerReferenceUnavailable", source)
        module_source = inspect.getsource(DurableTelegramClient)
        self.assertNotIn("client.get_input_entity =", module_source)

    def test_missing_peer_control_flow_bypasses_exception_handlers(self):
        self.assertTrue(issubclass(PeerReferenceUnavailable, BaseException))
        self.assertFalse(issubclass(PeerReferenceUnavailable, Exception))


if __name__ == "__main__":
    unittest.main()
