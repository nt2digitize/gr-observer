import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from telethon import types

from gr_observer.durable_peers import DurablePeerObserverMixin, WAIT_PREFIX


class Tx:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class Conn:
    def __init__(self, pool):
        self.pool = pool

    def transaction(self):
        return Tx()

    async def execute(self, query, *args):
        self.pool.executed.append((query, args))
        return "OK"


class Acquire:
    def __init__(self, pool):
        self.conn = Conn(pool)

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class Pool:
    def __init__(self):
        self.executed = []
        self.effect_row = None

    def acquire(self):
        return Acquire(self)

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"

    async def fetchrow(self, query, *args):
        if "SELECT status,last_error FROM telegram_effects" in query:
            return self.effect_row
        return None


class Storage:
    def __init__(self):
        self.begin_effect_original = AsyncMock(return_value=("execute", None))
        self.fail_action_original = AsyncMock()
        self.begin_effect = self.begin_effect_original
        self.fail_action = self.fail_action_original


class Base:
    def __init__(self):
        self.pool = Pool()
        self.storage = Storage()
        self.me = SimpleNamespace(id=999)
        self.user = None


class Harness(DurablePeerObserverMixin, Base):
    pass


class LegacyPeerRehydrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_peer_failure_is_parked_not_failed(self):
        app = Harness()
        await app._install_legacy_peer_wait_storage()
        await app.storage.fail_action(
            7,
            RuntimeError(
                "ValueError: Could not find the input entity for PeerUser(user_id=42)"
            ),
        )
        app.storage.fail_action_original.assert_not_awaited()
        sql = "\n".join(query for query, _ in app.pool.executed)
        self.assertIn("UPDATE telegram_effects", sql)
        self.assertIn("UPDATE outbox_actions", sql)
        self.assertTrue(
            any(
                any(str(value).startswith(WAIT_PREFIX) for value in args)
                for _, args in app.pool.executed
            )
        )

    async def test_waiting_effect_is_safe_to_execute_after_wakeup(self):
        app = Harness()
        app.pool.effect_row = {
            "status": "review",
            "last_error": f"{WAIT_PREFIX} process restarted while waiting",
        }
        await app._install_legacy_peer_wait_storage()
        result = await app.storage.begin_effect(
            effect_key="pv:42:send",
            action_id=7,
            effect_type="send_text",
            payload={"peer": "42"},
        )
        self.assertEqual(result, ("execute", None))
        app.storage.begin_effect_original.assert_not_awaited()
        self.assertTrue(
            any(
                "attempts=attempts+1" in query
                for query, _ in app.pool.executed
            )
        )

    async def test_real_inbound_peer_wakes_only_waiting_pv_work(self):
        app = Harness()
        event = SimpleNamespace(
            out=False,
            is_private=True,
            input_sender=types.InputPeerUser(user_id=42, access_hash=123456),
        )
        await app._remember_event_peer(event)
        sql = "\n".join(query for query, _ in app.pool.executed)
        self.assertIn("INSERT INTO telegram_peer_refs", sql)
        self.assertIn("payload->>'peer'=$1", sql)
        wake_args = [
            args
            for query, args in app.pool.executed
            if "payload->>'peer'=$1" in query
        ][0]
        self.assertEqual(wake_args[0], "42")

    async def test_non_peer_failure_keeps_original_failure_path(self):
        app = Harness()
        await app._install_legacy_peer_wait_storage()
        exc = RuntimeError("ordinary failure")
        await app.storage.fail_action(9, exc)
        app.storage.fail_action_original.assert_awaited_once_with(9, exc)


if __name__ == "__main__":
    unittest.main()
