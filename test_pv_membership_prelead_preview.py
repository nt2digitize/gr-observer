import unittest
from datetime import datetime, timezone

from gr_observer.group_membership_tracker import (
    GroupMembershipTracker,
    membership_change_from_update,
)


class ChannelParticipant:
    pass


class UpdateChannelParticipant:
    def __init__(self, *, channel_id=123, user_id=456, qts=1):
        self.prev_participant = None
        self.new_participant = ChannelParticipant()
        self.channel_id = channel_id
        self.user_id = user_id
        self.qts = qts
        self.invite = None
        self.date = datetime(2026, 9, 18, 11, 0, tzinfo=timezone.utc)


class FakeDb:
    def __init__(self, *, preview_chat_id=None):
        self.preview_chat_id = preview_chat_id
        self.events = set()
        self.state = {}

    def acquire(self):
        return self

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def fetchrow(self, query, *args):
        if "FROM pv_group_membership_state" in query:
            row = self.state.get((int(args[0]), int(args[1])))
            return row
        raise AssertionError(f"fetchrow inesperado: {query}")

    async def fetchval(self, query, *args):
        if "SELECT target_chat_id FROM link_targets" in query:
            return self.preview_chat_id
        if "FROM link_targets" in query:
            return self.preview_chat_id is not None and int(args[1]) == int(self.preview_chat_id)
        if "EXISTS(SELECT 1 FROM contact_ledger" in query:
            return False
        if "INSERT INTO pv_group_membership_events" in query:
            key = str(args[0])
            if key in self.events:
                return None
            self.events.add(key)
            return key
        if "SELECT last_telegram_order FROM pv_group_membership_state" in query:
            row = self.state.get((int(args[0]), int(args[1])))
            return None if row is None else row["last_telegram_order"]
        if "INSERT INTO pv_group_membership_state" in query and "RETURNING status" in query:
            chat_id, user_id, status, event_at = args
            key = (int(chat_id), int(user_id))
            row = self.state.get(key)
            if row is None:
                row = {
                    "status": str(status),
                    "preview_group": True,
                    "last_telegram_order": None,
                    "event_at": event_at,
                    "reason": "current_membership_reconcile",
                }
                self.state[key] = row
            else:
                row["preview_group"] = True
            return row["status"]
        raise AssertionError(f"fetchval inesperado: {query}")

    async def execute(self, query, *args):
        if "CREATE TABLE IF NOT EXISTS" in query:
            return "OK"
        if "SET preview_group=TRUE" in query:
            row = self.state[(int(args[0]), int(args[1]))]
            row["preview_group"] = True
            return "OK"
        if "INSERT INTO pv_group_membership_state" not in query:
            return "OK"
        chat_id, user_id, status, event_at, reason, preview_group, order = args
        self.state[(int(chat_id), int(user_id))] = {
            "status": str(status),
            "preview_group": bool(preview_group),
            "last_telegram_order": order,
            "event_at": event_at,
            "reason": reason,
        }
        return "OK"


class FakePermissions:
    def __init__(self, *, has_left=False):
        self.has_left = has_left


class FakeClient:
    def __init__(self, *, has_left=False):
        self.has_left = has_left
        self.calls = []

    async def get_permissions(self, chat_id, user_id):
        self.calls.append((int(chat_id), int(user_id)))
        return FakePermissions(has_left=self.has_left)


class PreviewMembershipBeforeLeadTests(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_user_joining_preview_is_persisted(self):
        update = UpdateChannelParticipant()
        change = membership_change_from_update(update)
        db = FakeDb(preview_chat_id=change.chat_id)
        tracker = GroupMembershipTracker(db, preview_link="https://t.me/+Preview")

        outcome = await tracker.observe(update)

        self.assertEqual(outcome, "joined")
        self.assertEqual(len(db.events), 1)
        row = db.state[(change.chat_id, change.user_id)]
        self.assertEqual(row["status"], "joined")
        self.assertTrue(row["preview_group"])

    async def test_unknown_user_outside_preview_remains_ignored(self):
        update = UpdateChannelParticipant()
        db = FakeDb(preview_chat_id=None)
        tracker = GroupMembershipTracker(db, preview_link="https://t.me/+Preview")

        outcome = await tracker.observe(update)

        self.assertEqual(outcome, "ignored_unknown_lead")
        self.assertFalse(db.events)
        self.assertFalse(db.state)

    async def test_missing_state_is_reconciled_from_current_telegram_membership(self):
        db = FakeDb(preview_chat_id=-100123)
        client = FakeClient(has_left=False)
        tracker = GroupMembershipTracker(db, preview_link="https://t.me/+Preview")

        outcome = await tracker.reconcile_current(client, 456)

        self.assertEqual(outcome, "joined")
        self.assertEqual(client.calls, [(-100123, 456)])
        self.assertEqual(db.state[(-100123, 456)]["status"], "joined")
        self.assertTrue(db.state[(-100123, 456)]["preview_group"])

    async def test_existing_state_avoids_extra_telegram_membership_call(self):
        db = FakeDb(preview_chat_id=-100123)
        db.state[(-100123, 456)] = {
            "status": "joined",
            "preview_group": False,
            "last_telegram_order": 4,
        }
        client = FakeClient(has_left=True)
        tracker = GroupMembershipTracker(db, preview_link="https://t.me/+Preview")

        outcome = await tracker.reconcile_current(client, 456)

        self.assertEqual(outcome, "joined")
        self.assertFalse(client.calls)
        self.assertTrue(db.state[(-100123, 456)]["preview_group"])


if __name__ == "__main__":
    unittest.main()
