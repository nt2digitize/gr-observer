import unittest
from types import SimpleNamespace

from telethon import types

from gr_observer.durable_peers import DurablePeerObserverMixin


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


class FakeClient:
    def __init__(self):
        self.calls = []

    async def get_input_entity(self, peer):
        self.calls.append(peer)
        if peer == "cached":
            return "cached-result"
        raise ValueError(f"missing entity {peer}")


class BaseObserver:
    def __init__(self):
        self.pool = FakePool()
        self.user = FakeClient()
        self.me = SimpleNamespace(id=999)
        self.connected = []
        self.dispatched = []

    async def _connect_module(self, module_id):
        self.connected.append(module_id)

    async def guarded_dispatch(self, event):
        self.dispatched.append(event)


class ObserverHarness(DurablePeerObserverMixin, BaseObserver):
    pass


class DurablePeerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_resolution_still_wins(self):
        app = ObserverHarness()
        await app._install_durable_peer_resolver()
        self.assertEqual(await app.user.get_input_entity("cached"), "cached-result")

    async def test_numeric_peer_falls_back_to_persisted_access_hash(self):
        app = ObserverHarness()
        app.pool.rows[(999, 42)] = 123456789
        await app._install_durable_peer_resolver()
        resolved = await app.user.get_input_entity(42)
        self.assertIsInstance(resolved, types.InputPeerUser)
        self.assertEqual(resolved.user_id, 42)
        self.assertEqual(resolved.access_hash, 123456789)

    async def test_peer_reference_is_scoped_to_operator_account(self):
        app = ObserverHarness()
        app.pool.rows[(111, 42)] = 123456789
        await app._install_durable_peer_resolver()
        with self.assertRaises(ValueError):
            await app.user.get_input_entity(42)

    async def test_inbound_private_input_sender_is_persisted(self):
        app = ObserverHarness()
        event = SimpleNamespace(
            out=False,
            is_private=True,
            input_sender=types.InputPeerUser(user_id=42, access_hash=987654321),
        )
        await app.guarded_dispatch(event)
        self.assertEqual(app.pool.rows[(999, 42)], 987654321)
        self.assertEqual(app.dispatched, [event])

    async def test_zero_hash_is_not_persisted_and_event_still_dispatches(self):
        app = ObserverHarness()
        event = SimpleNamespace(
            out=False,
            is_private=True,
            input_sender=types.InputPeerUser(user_id=42, access_hash=0),
        )
        await app.guarded_dispatch(event)
        self.assertNotIn((999, 42), app.pool.rows)
        self.assertEqual(app.dispatched, [event])

    async def test_resolver_is_installed_before_module_connect(self):
        app = ObserverHarness()
        app.pool.rows[(999, 77)] = 222333444
        await app._connect_module("pv_reply")
        resolved = await app.user.get_input_entity(77)
        self.assertEqual(resolved.access_hash, 222333444)
        self.assertEqual(app.connected, ["pv_reply"])


if __name__ == "__main__":
    unittest.main()
