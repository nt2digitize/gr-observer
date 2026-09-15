import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gr_observer.modules.group_reply_contacts import GroupReplyWithContacts


class GroupSingleVisibleMessageTests(unittest.IsolatedAsyncioTestCase):
    def module(self):
        pool = SimpleNamespace(
            fetchrow=AsyncMock(),
            fetchval=AsyncMock(),
            execute=AsyncMock(),
        )
        storage = SimpleNamespace(pool=pool)
        module = GroupReplyWithContacts(storage, SimpleNamespace())
        module._show_human_typing = AsyncMock()
        module._in_cooldown = AsyncMock(return_value=False)
        module.contact_flow.clear_protection = AsyncMock()
        return module, pool

    async def test_reactive_reply_replaces_current_loop_message(self):
        module, pool = self.module()
        pool.fetchrow.side_effect = [
            {"status": "queued"},
            {"current_message_id": 10, "template_version": 2},
            {"current_message_id": 10, "template_version": 2},
        ]
        effects = SimpleNamespace(
            send_text=AsyncMock(return_value={"message_id": 20}),
            delete_messages=AsyncMock(return_value={"deleted": True}),
        )
        action = {
            "action_key": "group-reply:-100:77",
            "payload": {
                "peer": -100,
                "source_message_id": 77,
                "text": "chama no pv",
            },
        }
        result = await module.action_send_group_reply(action, effects)
        self.assertTrue(result["sent"])
        effects.delete_messages.assert_awaited_once_with(
            -100, [10], "group-reply:-100:77:delete-old"
        )
        module.contact_flow.clear_protection.assert_awaited_once_with(-100, 10)
        updates = [call.args for call in pool.execute.await_args_list]
        self.assertTrue(
            any(
                "UPDATE group_repost_state SET current_message_id=$2" in args[0]
                and args[1:] == (-100, 20, 2)
                for args in updates
            )
        )

    async def test_reactive_only_group_replaces_previous_reactive_reply(self):
        module, pool = self.module()
        pool.fetchrow.side_effect = [
            {"status": "queued"},
            None,
        ]
        pool.fetchval.return_value = 9
        effects = SimpleNamespace(
            send_text=AsyncMock(return_value={"message_id": 20}),
            delete_messages=AsyncMock(return_value={"deleted": True}),
        )
        action = {
            "action_key": "group-reply:-100:78",
            "payload": {
                "peer": -100,
                "source_message_id": 78,
                "text": "vem no pv",
            },
        }
        result = await module.action_send_group_reply(action, effects)
        self.assertTrue(result["sent"])
        effects.delete_messages.assert_awaited_once_with(
            -100, [9], "group-reply:-100:78:delete-old"
        )

    async def test_repost_deletes_old_message_even_if_engagement_was_protected(self):
        module, pool = self.module()
        pool.fetchrow.return_value = {
            "template_text": "meu texto",
            "current_message_id": 10,
            "template_version": 3,
            "repost_pending": True,
            "repost_approved_at": object(),
        }
        pool.fetchval.return_value = 3
        module._reply_blocks_repost = AsyncMock(return_value=False)
        module.contact_flow.is_message_protected = AsyncMock(return_value=True)
        effects = SimpleNamespace(
            send_text=AsyncMock(return_value={"message_id": 20}),
            delete_messages=AsyncMock(return_value={"deleted": True}),
        )
        action = {
            "action_key": "group-repost:-100:3:90",
            "payload": {"peer": -100, "template_version": 3},
        }
        result = await module.action_repost_group_text(action, effects)
        self.assertTrue(result["sent"])
        effects.delete_messages.assert_awaited_once_with(
            -100, [10], "group-repost:-100:3:90:delete-old"
        )
        module.contact_flow.is_message_protected.assert_not_awaited()
        module.contact_flow.clear_protection.assert_awaited_once_with(-100, 10)

    async def test_newer_manual_message_wins_over_delayed_reactive_reply(self):
        module, pool = self.module()
        pool.fetchrow.side_effect = [
            {"status": "queued"},
            {"current_message_id": 10, "template_version": 2},
            {"current_message_id": 15, "template_version": 3},
        ]
        effects = SimpleNamespace(
            send_text=AsyncMock(return_value={"message_id": 20}),
            delete_messages=AsyncMock(return_value={"deleted": True}),
        )
        action = {
            "action_key": "group-reply:-100:79",
            "payload": {
                "peer": -100,
                "source_message_id": 79,
                "text": "chama no privado",
            },
        }
        result = await module.action_send_group_reply(action, effects)
        self.assertFalse(result["sent"])
        self.assertEqual(result["reason"], "newer_group_message")
        effects.delete_messages.assert_awaited_once_with(
            -100, [20], "group-reply:-100:79:delete-stale"
        )
        module.contact_flow.clear_protection.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
