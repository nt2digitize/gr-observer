import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gr_observer.modules.group_reply import GroupReplyModule, _delay_seconds


class GroupReplyDelayTests(unittest.TestCase):
    def module(self):
        storage = SimpleNamespace(pool=object())
        return GroupReplyModule(storage, SimpleNamespace())

    def test_default_reply_delay_is_between_70_and_90_seconds(self):
        with patch.dict(os.environ, {}, clear=True):
            module = self.module()

        self.assertEqual(module.min_delay, 70)
        self.assertEqual(module.max_delay, 90)
        self.assertEqual(module.min_cooldown, 95)
        self.assertEqual(module.max_cooldown, 110)
        for event_id in range(100):
            delay = _delay_seconds(f"-100123:{event_id}", module.min_delay, module.max_delay)
            self.assertGreaterEqual(delay, 70)
            self.assertLessEqual(delay, 90)
            cooldown = _delay_seconds(
                f"cooldown:-100123:{event_id}",
                module.min_cooldown,
                module.max_cooldown,
            )
            self.assertGreaterEqual(cooldown, 95)
            self.assertLessEqual(cooldown, 110)

    def test_environment_can_override_reply_delay(self):
        values = {
            "GROUP_REPLY_MIN_DELAY_SECONDS": "75",
            "GROUP_REPLY_MAX_DELAY_SECONDS": "85",
            "GROUP_REPLY_MIN_COOLDOWN_SECONDS": "100",
            "GROUP_REPLY_MAX_COOLDOWN_SECONDS": "105",
        }
        with patch.dict(os.environ, values, clear=True):
            module = self.module()

        self.assertEqual(module.min_delay, 75)
        self.assertEqual(module.max_delay, 85)
        self.assertEqual(module.min_cooldown, 100)
        self.assertEqual(module.max_cooldown, 105)


class GroupRepostTests(unittest.IsolatedAsyncioTestCase):
    def module(self):
        pool = type("Pool", (), {})()
        pool.execute = AsyncMock()
        storage = SimpleNamespace(pool=pool)
        return GroupReplyModule(storage, SimpleNamespace()), pool

    async def test_manual_text_authorizes_chat_and_becomes_template(self):
        module, pool = self.module()
        event = SimpleNamespace(raw_text="meu texto", media=None, id=77)
        entity = SimpleNamespace(id=1001, title="Grupo teste", username="grupo_teste", kind="group")
        with patch(
            "gr_observer.modules.group_reply.utils.get_peer_id", return_value=-1001
        ):
            handled = await module._record_manual_template(event, entity)

        self.assertTrue(handled)
        self.assertIn(-1001, module.dynamic_chat_ids)
        self.assertEqual(pool.execute.await_count, 2)
        template_args = pool.execute.await_args_list[0].args
        self.assertEqual(template_args[-2:], ("meu texto", 77))
        chat_args = pool.execute.await_args_list[1].args
        self.assertEqual(chat_args[1:5], (-1001, "Grupo teste", "grupo_teste", "group"))

    async def test_automated_text_does_not_replace_manual_template(self):
        module, pool = self.module()
        module._mark_automated_outbound(-1001, "resposta automática")
        event = SimpleNamespace(raw_text="resposta automática", media=None, id=78)
        with patch(
            "gr_observer.modules.group_reply.utils.get_peer_id", return_value=-1001
        ):
            handled = await module._record_manual_template(event, object())

        self.assertTrue(handled)
        pool.execute.assert_not_awaited()

    async def test_repost_sends_new_copy_before_deleting_old_copy(self):
        module, pool = self.module()
        pool.fetchrow = AsyncMock(
            return_value={
                "template_text": "meu texto",
                "current_message_id": 10,
                "template_version": 3,
                "repost_pending": True,
            }
        )
        pool.fetchval = AsyncMock(return_value=3)
        calls = []

        async def send_text(*_args):
            calls.append("send")
            return {"message_id": 20}

        async def delete_messages(*_args):
            calls.append("delete")
            return {"deleted": True}

        effects = SimpleNamespace(
            send_text=send_text,
            delete_messages=delete_messages,
        )
        result = await module.action_repost_group_text(
            {
                "action_key": "repost-1",
                "payload": {"peer": -1001, "template_version": 3},
            },
            effects,
        )

        self.assertEqual(calls, ["send", "delete"])
        self.assertTrue(result["sent"])
        update_args = pool.execute.call_args.args
        self.assertEqual(update_args[-3:], (-1001, 20, 3))

    async def test_tenth_new_human_message_queues_one_repost(self):
        state = {
            "template_text": "meu texto",
            "current_message_id": 10,
            "inbound_count": 0,
            "template_version": 1,
            "repost_pending": False,
        }
        actions = []

        class Context:
            def __init__(self, value):
                self.value = value

            async def __aenter__(self):
                return self.value

            async def __aexit__(self, *_args):
                return False

        class Connection:
            def transaction(self):
                return Context(self)

            async def fetchrow(self, *_args):
                return dict(state)

            async def execute(self, query, *args):
                if "SET inbound_count=$2" in query:
                    state["inbound_count"] = int(args[1])
                elif "SET inbound_count=0" in query:
                    state["inbound_count"] = 0
                    state["repost_pending"] = True
                elif "INSERT INTO outbox_actions" in query:
                    actions.append(args)

        connection = Connection()
        module, _pool = self.module()
        module.pool.acquire = lambda: Context(connection)
        module.dynamic_chat_ids.add(-1001)

        for message_id in range(1, 11):
            await module._count_and_queue_repost(-1001, message_id)

        self.assertEqual(len(actions), 1)
        self.assertTrue(state["repost_pending"])
        self.assertEqual(state["inbound_count"], 0)


if __name__ == "__main__":
    unittest.main()
