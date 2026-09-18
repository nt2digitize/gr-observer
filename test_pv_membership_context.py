import inspect
import unittest

from gr_observer.modules.pv_reply_production import PvReplyProduction
from gr_observer.pv_membership_context import (
    BEFORE_JOIN,
    JOINED,
    LEFT,
    REJOINED,
    membership_conversation_context,
)


class FakePool:
    def __init__(self, row=None):
        self.row = row
        self.query = None
        self.args = None

    async def fetchrow(self, query, *args):
        self.query = query
        self.args = args
        return self.row


class MembershipConversationContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_before_join_when_no_preview_membership_exists(self):
        pool = FakePool(None)
        context = await membership_conversation_context(pool, 42)
        self.assertEqual(context.state, BEFORE_JOIN)
        self.assertEqual(context.allowed_repertoires, ("pre_join",))
        self.assertEqual(pool.args, (42,))

    async def test_first_join_uses_joined_repertoire(self):
        pool = FakePool({"status": "joined", "join_count": 1, "leave_count": 0})
        context = await membership_conversation_context(pool, 42)
        self.assertEqual(context.state, JOINED)
        self.assertEqual(context.allowed_repertoires, ("joined", "post_join"))

    async def test_exit_uses_left_repertoire(self):
        pool = FakePool({"status": "left", "join_count": 1, "leave_count": 1})
        context = await membership_conversation_context(pool, 42)
        self.assertEqual(context.state, LEFT)
        self.assertEqual(context.allowed_repertoires, ("left", "post_exit"))

    async def test_reentry_uses_rejoined_repertoire(self):
        pool = FakePool({"status": "joined", "join_count": 2, "leave_count": 1})
        context = await membership_conversation_context(pool, 42)
        self.assertEqual(context.state, REJOINED)
        self.assertEqual(context.allowed_repertoires, ("rejoined", "post_join"))

    async def test_only_preview_group_is_considered(self):
        pool = FakePool(None)
        await membership_conversation_context(pool, 42)
        self.assertIn("preview_group IS TRUE", pool.query)
        self.assertNotIn("link_targets", pool.query)


class MembershipConversationContextSafetyTests(unittest.TestCase):
    def test_context_reader_cannot_mutate_or_advance_flow(self):
        source = inspect.getsource(membership_conversation_context).upper()
        for token in (
            "INSERT ",
            "UPDATE ",
            "DELETE ",
            "OUTBOX",
            "TELEGRAM",
            "NEXT_STEP",
            "PV_LINEAR_SESSIONS",
            "SEND_MESSAGE",
        ):
            self.assertNotIn(token, source)

    def test_send_hook_only_attaches_context_and_does_not_advance_lane(self):
        source = inspect.getsource(PvReplyProduction._send_row)
        self.assertIn("membership_conversation_context", source)
        self.assertIn("_membership_state", source)
        self.assertIn("_membership_repertoires", source)
        for token in (
            "next_step",
            "pv_linear_sessions",
            "outbox_actions",
            "_queue_linear_step",
        ):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
