import inspect
import unittest
from datetime import datetime, timezone

from gr_observer.group_membership_tracker import (
    DDL,
    GroupMembershipTracker,
    _is_stale_order,
    membership_change_from_update,
)
from gr_observer.modules.pv_reply_production import PvReplyProduction


class ChannelParticipant:
    pass


class ChannelParticipantLeft:
    pass


class ChannelParticipantBanned:
    def __init__(self, *, left: bool):
        self.left = left


class Invite:
    def __init__(self, link: str):
        self.link = link


class UpdateChannelParticipant:
    def __init__(
        self,
        *,
        prev,
        new,
        channel_id=123,
        user_id=456,
        actor_id=456,
        qts=77,
        invite=None,
    ):
        self.prev_participant = prev
        self.new_participant = new
        self.channel_id = channel_id
        self.user_id = user_id
        self.actor_id = actor_id
        self.qts = qts
        self.invite = invite
        self.date = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)


class UpdateChatParticipantAdd:
    def __init__(self):
        self.chat_id = 22
        self.user_id = 456
        self.inviter_id = 999
        self.version = 8
        self.date = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)


class UpdateChatParticipantDelete:
    def __init__(self):
        self.chat_id = 22
        self.user_id = 456
        self.version = 9


class FakeMembershipDb:
    def __init__(self, *, known=True, preview_chat_id=None):
        self.known = known
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
        if "EXISTS(SELECT 1 FROM contact_ledger" in query:
            return self.known
        if "SELECT EXISTS(" in query and "FROM link_targets" in query:
            return self.preview_chat_id is not None and int(args[1]) == int(self.preview_chat_id)
        if "INSERT INTO pv_group_membership_events" in query:
            event_key = str(args[0])
            if event_key in self.events:
                return None
            self.events.add(event_key)
            return event_key
        if "SELECT last_telegram_order FROM pv_group_membership_state" in query:
            row = self.state.get((int(args[0]), int(args[1])))
            return None if row is None else row["last_telegram_order"]
        raise AssertionError(f"fetchval inesperado: {query}")

    async def execute(self, query, *args):
        if query is DDL or "CREATE TABLE IF NOT EXISTS" in query:
            return "OK"
        if "INSERT INTO pv_group_membership_state" not in query:
            return "OK"
        chat_id, user_id, status, event_at, reason, preview_group, order = args
        key = (int(chat_id), int(user_id))
        row = self.state.get(key)
        if row is None:
            row = {
                "status": str(status),
                "join_count": 0,
                "leave_count": 0,
                "preview_group": False,
                "last_telegram_order": None,
            }
            self.state[key] = row
        row["status"] = str(status)
        row["join_count"] += int(status == "joined")
        row["leave_count"] += int(status == "left")
        row["preview_group"] = bool(row["preview_group"] or preview_group)
        row["last_telegram_order"] = order
        row["last_change_at"] = event_at
        row["last_reason"] = reason
        return "OK"


class MembershipClassifierTests(unittest.TestCase):
    def test_join_marks_preview_group_when_invite_matches(self):
        link = "https://t.me/+PreviewABC"
        change = membership_change_from_update(
            UpdateChannelParticipant(
                prev=None,
                new=ChannelParticipant(),
                invite=Invite(link),
            ),
            preview_link=link + "/",
        )
        self.assertIsNotNone(change)
        self.assertEqual(change.transition, "joined")
        self.assertTrue(change.preview_link_match)
        self.assertEqual(change.user_id, 456)
        self.assertLess(change.chat_id, 0)

    def test_member_to_admin_like_state_is_not_a_membership_change(self):
        change = membership_change_from_update(
            UpdateChannelParticipant(
                prev=ChannelParticipant(),
                new=ChannelParticipant(),
            )
        )
        self.assertIsNone(change)

    def test_restricted_member_is_not_mistaken_for_exit(self):
        change = membership_change_from_update(
            UpdateChannelParticipant(
                prev=ChannelParticipant(),
                new=ChannelParticipantBanned(left=False),
            )
        )
        self.assertIsNone(change)

    def test_kicked_or_banned_left_is_exit(self):
        change = membership_change_from_update(
            UpdateChannelParticipant(
                prev=ChannelParticipant(),
                new=ChannelParticipantBanned(left=True),
            )
        )
        self.assertIsNotNone(change)
        self.assertEqual(change.transition, "left")
        self.assertEqual(change.reason, "kicked_or_banned")

    def test_explicit_channel_left_is_exit(self):
        change = membership_change_from_update(
            UpdateChannelParticipant(
                prev=ChannelParticipant(),
                new=ChannelParticipantLeft(),
            )
        )
        self.assertIsNotNone(change)
        self.assertEqual(change.transition, "left")

    def test_basic_group_add_and_delete_are_supported(self):
        joined = membership_change_from_update(UpdateChatParticipantAdd())
        left = membership_change_from_update(UpdateChatParticipantDelete())
        self.assertEqual(joined.transition, "joined")
        self.assertEqual(left.transition, "left")
        self.assertEqual(joined.user_id, left.user_id)
        self.assertEqual(joined.chat_id, left.chat_id)

    def test_out_of_order_telegram_sequence_is_stale(self):
        self.assertTrue(_is_stale_order(12, 11))
        self.assertTrue(_is_stale_order(12, 12))
        self.assertFalse(_is_stale_order(12, 13))
        self.assertFalse(_is_stale_order(None, 13))
        self.assertFalse(_is_stale_order(12, None))


class MembershipPersistenceSimulationTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_duplicate_leave_rejoin_sequence(self):
        db = FakeMembershipDb()
        tracker = GroupMembershipTracker(db)
        join = UpdateChannelParticipant(prev=None, new=ChannelParticipant(), qts=1)
        leave = UpdateChannelParticipant(
            prev=ChannelParticipant(), new=ChannelParticipantLeft(), qts=2
        )
        rejoin = UpdateChannelParticipant(
            prev=ChannelParticipantLeft(), new=ChannelParticipant(), qts=3
        )

        self.assertEqual(await tracker.observe(join), "joined")
        self.assertEqual(await tracker.observe(join), "duplicate")
        self.assertEqual(await tracker.observe(leave), "left")
        self.assertEqual(await tracker.observe(rejoin), "joined")

        self.assertEqual(len(db.events), 3)
        row = next(iter(db.state.values()))
        self.assertEqual(row["status"], "joined")
        self.assertEqual(row["join_count"], 2)
        self.assertEqual(row["leave_count"], 1)
        self.assertEqual(row["last_telegram_order"], 3)

    async def test_late_update_does_not_regress_current_state(self):
        db = FakeMembershipDb()
        tracker = GroupMembershipTracker(db)
        newest = UpdateChannelParticipant(prev=None, new=ChannelParticipant(), qts=5)
        late = UpdateChannelParticipant(
            prev=ChannelParticipant(), new=ChannelParticipantLeft(), qts=4
        )

        self.assertEqual(await tracker.observe(newest), "joined")
        self.assertEqual(await tracker.observe(late), "stale")
        row = next(iter(db.state.values()))
        self.assertEqual(row["status"], "joined")
        self.assertEqual(row["join_count"], 1)
        self.assertEqual(row["leave_count"], 0)
        self.assertEqual(row["last_telegram_order"], 5)
        self.assertEqual(len(db.events), 2)

    async def test_unknown_lead_is_not_persisted(self):
        db = FakeMembershipDb(known=False)
        tracker = GroupMembershipTracker(db)
        join = UpdateChannelParticipant(prev=None, new=ChannelParticipant(), qts=1)

        self.assertEqual(await tracker.observe(join), "ignored_unknown_lead")
        self.assertFalse(db.events)
        self.assertFalse(db.state)

    async def test_known_link_target_marks_preview_group_without_invite_in_update(self):
        probe = membership_change_from_update(
            UpdateChannelParticipant(prev=None, new=ChannelParticipant(), qts=10)
        )
        db = FakeMembershipDb(preview_chat_id=probe.chat_id)
        tracker = GroupMembershipTracker(db, preview_link="https://t.me/+PreviewABC")

        self.assertEqual(
            await tracker.observe(
                UpdateChannelParticipant(prev=None, new=ChannelParticipant(), qts=10)
            ),
            "joined",
        )
        row = next(iter(db.state.values()))
        self.assertTrue(row["preview_group"])


class MembershipSafetyContractTests(unittest.TestCase):
    def test_tracker_observe_has_no_sending_surface(self):
        source = inspect.getsource(GroupMembershipTracker.observe)
        self.assertNotIn("outbox", source.casefold())
        self.assertNotIn("send_message", source)
        self.assertNotIn("effects.", source)

    def test_shadow_does_not_persist_raw_invite_or_actor_identity(self):
        ddl = DDL.casefold()
        self.assertNotIn("invite_link", ddl)
        self.assertNotIn("actor_id", ddl)

    def test_preview_chat_lookup_reuses_existing_link_targets(self):
        source = inspect.getsource(GroupMembershipTracker._known_preview_chat)
        self.assertIn("link_targets", source)
        self.assertIn("target_chat_id", source)
        for token in ("INSERT ", "UPDATE ", "DELETE "):
            self.assertNotIn(token, source.upper())

    def test_stale_event_is_recorded_but_cannot_regress_current_state(self):
        source = inspect.getsource(GroupMembershipTracker.observe)
        event_insert = source.index("INSERT INTO pv_group_membership_events")
        stale_guard = source.index("_is_stale_order")
        state_upsert = source.index("INSERT INTO pv_group_membership_state")
        self.assertLess(event_insert, stale_guard)
        self.assertLess(stale_guard, state_upsert)

    def test_integration_reuses_existing_pv_client(self):
        source = inspect.getsource(PvReplyProduction)
        self.assertIn("GroupMembershipTracker", source)
        self.assertIn("events.Raw", source)
        self.assertIn("UpdateChannelParticipant", source)
        self.assertNotIn("TelegramClient(", source)
        self.assertNotIn("SafeOutboxWriter", source)

    def test_membership_failures_are_isolated(self):
        source = inspect.getsource(PvReplyProduction._on_membership_update)
        self.assertIn("except Exception", source)
        self.assertNotIn("pause_module", source)

    def test_membership_handler_is_removed_on_disconnect(self):
        source = inspect.getsource(PvReplyProduction.on_disconnect)
        self.assertIn("remove_event_handler(self._membership_handler)", source)
        self.assertIn("self._membership_handler = None", source)


if __name__ == "__main__":
    unittest.main()
