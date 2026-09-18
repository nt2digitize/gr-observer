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

    async def fetchval(self, query, *args):
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
        raise AssertionError(f"fetchval inesperado: {query}")

    async def execute(self, query, *args):
        if "CREATE TABLE IF NOT EXISTS" in query:
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


if __name__ == "__main__":
    unittest.main()
