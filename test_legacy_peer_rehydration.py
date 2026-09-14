import unittest
from types import SimpleNamespace

from telethon import types

from gr_observer.telegram_peers import DurablePeerStore, WAIT_PREFIX


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
        self.reopen = True

    def acquire(self):
        return Acquire(self)

    async def execute(self, query, *args):
        self.executed.append((query, args))
        return "OK"

    async def fetchval(self, query, *args):
        self.executed.append((query, args))
        if "RETURNING effect_key" in query and self.reopen:
            return args[0]
        return None

    async def fetchrow(self, query, *args):
        return None


class LegacyPeerRehydrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_peer_action_is_parked_not_failed(self):
        pool = Pool()
        store = DurablePeerStore(pool)
        await store.park_action(7, 42)
        query, args = pool.executed[-1]
        self.assertIn("status='pending'", query)
        self.assertIn("available_at", query)
        self.assertTrue(str(args[1]).startswith(WAIT_PREFIX))
        self.assertNotIn("status='failed'", query)

    async def test_waiting_effect_is_explicitly_reopened(self):
        pool = Pool()
        store = DurablePeerStore(pool)
        self.assertTrue(await store.reopen_waiting_effect("pv:42:send"))
        query, args = pool.executed[-1]
        self.assertIn("attempts=attempts+1", query)
        self.assertIn("last_error=NULL", query)
        self.assertEqual(args[0], "pv:42:send")

    async def test_real_inbound_peer_wakes_only_waiting_pv_work(self):
        pool = Pool()
        store = DurablePeerStore(pool)
        store.account_user_id = 999
        event = SimpleNamespace(
            out=False,
            is_private=True,
            input_sender=types.InputPeerUser(user_id=42, access_hash=123456),
        )
        await store.remember_event(event)
        sql = "\n".join(query for query, _ in pool.executed)
        self.assertIn("INSERT INTO telegram_peer_refs", sql)
        self.assertIn("payload->>'peer'=$1", sql)

    async def test_legacy_recovery_is_non_destructive(self):
        pool = Pool()
        store = DurablePeerStore(pool)
        store.account_user_id = 999
        await store.recover_legacy_waits()
        sql = "\n".join(query for query, _ in pool.executed).upper()
        self.assertNotIn("DELETE FROM", sql)
        self.assertNotIn("TRUNCATE", sql)
        self.assertIn("UPDATE TELEGRAM_EFFECTS", sql)
        self.assertIn("UPDATE OUTBOX_ACTIONS", sql)


if __name__ == "__main__":
    unittest.main()
