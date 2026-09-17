import inspect
import unittest
from datetime import datetime, timezone

from gr_observer.application_membership import MembershipObserver
from gr_observer.group_membership_tracker import (
    GroupMembershipTracker,
    membership_change_from_update,
)


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


class MembershipSafetyContractTests(unittest.TestCase):
    def test_tracker_observe_has_no_sending_surface(self):
        source = inspect.getsource(GroupMembershipTracker.observe)
        self.assertNotIn("outbox", source.casefold())
        self.assertNotIn("send_message", source)
        self.assertNotIn("effects.", source)
        self.assertNotIn("enqueue", source)

    def test_integration_reuses_existing_user_runtime(self):
        source = inspect.getsource(MembershipObserver)
        self.assertIn("await super().user_runtime()", source)
        self.assertIn("events.Raw", source)
        self.assertNotIn("TelegramClient(", source)
        self.assertNotIn("SafeOutboxWriter", source)

    def test_membership_failures_are_isolated(self):
        source = inspect.getsource(MembershipObserver._observe_membership_update)
        self.assertIn("except Exception", source)
        self.assertNotIn("pause_module", source)


if __name__ == "__main__":
    unittest.main()
