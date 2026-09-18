import inspect
import unittest
from types import SimpleNamespace

from telethon import types, utils

from gr_observer.pv_membership_reconcile import (
    _persist_snapshot,
    reconcile_preview_membership,
)


class ChannelParticipant:
    pass


class FakeDb:
    def __init__(self, chat_id: int):
        self.chat_id = int(chat_id)
        self.state = {}
        self.events = []

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetch(self, query, *args):
        if "FROM link_targets" in query:
            return [{"target_chat_id": self.chat_id}]
        raise AssertionError(f"fetch inesperado: {query}")

    async def fetchrow(self, query, *args):
        if "FROM pv_group_membership_state" in query:
            return self.state.get((int(args[0]), int(args[1])))
        raise AssertionError(f"fetchrow inesperado: {query}")

    async def execute(self, query, *args):
        if "INSERT INTO pv_group_membership_events" in query:
            self.events.append(
                {
                    "event_key": str(args[0]),
                    "chat_id": int(args[1]),
                    "user_id": int(args[2]),
                    "status": str(args[3]),
                }
            )
            return "OK"

        if "INSERT INTO pv_group_membership_state" in query:
            chat_id, user_id, when = int(args[0]), int(args[1]), args[2]
            self.state[(chat_id, user_id)] = {
                "status": "joined",
                "join_count": 1,
                "leave_count": 0,
                "preview_group": True,
                "last_change_at": when,
            }
            return "OK"

        if "SET preview_group=TRUE" in query:
            row = self.state[(int(args[0]), int(args[1]))]
            row["preview_group"] = True
            return "OK"

        if "UPDATE pv_group_membership_state" in query:
            chat_id, user_id, status, when = (
                int(args[0]),
                int(args[1]),
                str(args[2]),
                args[3],
            )
            row = self.state[(chat_id, user_id)]
            row["status"] = status
            row["last_change_at"] = when
            row["join_count"] += int(status == "joined")
            row["leave_count"] += int(status == "left")
            row["preview_group"] = True
            return "OK"

        raise AssertionError(f"execute inesperado: {query}")


class FakeClient:
    def __init__(self, present=True):
        self.present = bool(present)
        self.calls = 0

    async def get_input_entity(self, entity):
        return entity

    async def __call__(self, request):
        self.calls += 1
        if not self.present:
            raise AssertionError("absence path not exercised in this fake")
        return SimpleNamespace(participant=ChannelParticipant())


class ReconcilePersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_absence_keeps_before_join_semantics(self):
        chat_id = utils.get_peer_id(types.PeerChannel(123))
        db = FakeDb(chat_id)
        outcome = await _persist_snapshot(
            db,
            chat_id=chat_id,
            user_id=456,
            present=False,
        )
        self.assertEqual(outcome, "absent_unknown")
        self.assertFalse(db.state)
        self.assertFalse(db.events)

    async def test_first_confirmed_presence_creates_joined_state_once(self):
        chat_id = utils.get_peer_id(types.PeerChannel(123))
        db = FakeDb(chat_id)

        first = await _persist_snapshot(
            db,
            chat_id=chat_id,
            user_id=456,
            present=True,
        )
        second = await _persist_snapshot(
            db,
            chat_id=chat_id,
            user_id=456,
            present=True,
        )

        row = db.state[(chat_id, 456)]
        self.assertEqual(first, "joined")
        self.assertEqual(second, "confirmed_joined")
        self.assertEqual(row["status"], "joined")
        self.assertEqual(row["join_count"], 1)
        self.assertEqual(row["leave_count"], 0)
        self.assertEqual(len(db.events), 1)

    async def test_left_to_joined_is_counted_as_rejoin(self):
        chat_id = utils.get_peer_id(types.PeerChannel(123))
        db = FakeDb(chat_id)
        db.state[(chat_id, 456)] = {
            "status": "left",
            "join_count": 1,
            "leave_count": 1,
            "preview_group": True,
            "last_change_at": None,
        }

        outcome = await _persist_snapshot(
            db,
            chat_id=chat_id,
            user_id=456,
            present=True,
        )

        row = db.state[(chat_id, 456)]
        self.assertEqual(outcome, "joined")
        self.assertEqual(row["status"], "joined")
        self.assertEqual(row["join_count"], 2)
        self.assertEqual(row["leave_count"], 1)

    async def test_reconcile_uses_existing_client_and_resolved_preview_target(self):
        chat_id = utils.get_peer_id(types.PeerChannel(123))
        db = FakeDb(chat_id)
        client = FakeClient(present=True)
        sender = SimpleNamespace(id=456)

        outcome = await reconcile_preview_membership(
            db,
            client,
            preview_link="https://t.me/+PreviewABC",
            user_id=456,
            user_entity=sender,
        )

        self.assertEqual(outcome, "joined")
        self.assertEqual(client.calls, 1)
        self.assertEqual(db.state[(chat_id, 456)]["status"], "joined")


class ReconcileSafetyTests(unittest.TestCase):
    def test_reconcile_module_never_creates_another_telegram_client(self):
        import gr_observer.pv_membership_reconcile as module

        source = inspect.getsource(module)
        self.assertNotIn("TelegramClient(", source)
        self.assertNotIn("ImportChatInviteRequest", source)
        self.assertNotIn("JoinChannelRequest", source)


if __name__ == "__main__":
    unittest.main()
