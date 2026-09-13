import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gr_observer.group_cadence import RepostPlan
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
        self.assertEqual(module.repost_reply_guard_seconds, 180)
        self.assertEqual(module.repost_max_reply_defer_seconds, 900)
        for event_id in range(100):
            delay = _delay_seconds(f"-100123:{event_id}", module.min_delay, module.max_delay)
            self.assertGreaterEqual(delay, 70)
            self.assertLessEqual(delay, 90)

    def test_environment_can_override_reply_and_arbiter_delays(self):
        values = {
            "GROUP_REPLY_MIN_DELAY_SECONDS": "75",
            "GROUP_REPLY_MAX_DELAY_SECONDS": "85",
            "GROUP_REPLY_MIN_COOLDOWN_SECONDS": "100",
            "GROUP_REPLY_MAX_COOLDOWN_SECONDS": "105",
            "GROUP_REPOST_REPLY_GUARD_SECONDS": "240",
            "GROUP_REPOST_MAX_REPLY_DEFER_SECONDS": "1200",
            "GROUP_REPOST_TIMEZONE": "America/Sao_Paulo",
        }
        with patch.dict(os.environ, values, clear=True):
            module = self.module()
        self.assertEqual(module.min_delay, 75)
        self.assertEqual(module.max_delay, 85)
        self.assertEqual(module.min_cooldown, 100)
        self.assertEqual(module.max_cooldown, 105)
        self.assertEqual(module.repost_reply_guard_seconds, 240)
        self.assertEqual(module.repost_max_reply_defer_seconds, 1200)


class GroupRepostTests(unittest.IsolatedAsyncioTestCase):
    def module(self):
        pool = type("Pool", (), {})()
        pool.execute = AsyncMock()
        storage = SimpleNamespace(pool=pool)
        return GroupReplyModule(storage, SimpleNamespace()), pool

    async def test_manual_text_authorizes_chat_and_resets_cycle_plan(self):
        module, pool = self.module()
        event = SimpleNamespace(raw_text="meu texto", media=None, id=77)
        entity = SimpleNamespace(id=1001, title="Grupo teste", username="grupo_teste", kind="group")
        with patch("gr_observer.modules.group_reply.utils.get_peer_id", return_value=-1001):
            handled = await module._record_manual_template(event, entity)
        self.assertTrue(handled)
        self.assertIn(-1001, module.dynamic_chat_ids)
        self.assertEqual(pool.execute.await_count, 2)
        sql = pool.execute.await_args_list[0].args[0]
        self.assertIn("cycle_target_messages=NULL", sql)
        self.assertIn("last_post_at=NOW()", sql)

    async def test_automated_text_does_not_replace_manual_template(self):
        module, pool = self.module()
        module._mark_automated_outbound(-1001, "resposta automática")
        event = SimpleNamespace(raw_text="resposta automática", media=None, id=78)
        with patch("gr_observer.modules.group_reply.utils.get_peer_id", return_value=-1001):
            handled = await module._record_manual_template(event, object())
        self.assertTrue(handled)
        pool.execute.assert_not_awaited()

    async def test_persisted_target_and_time_gate_queue_one_repost(self):
        state = {
            "template_text": "meu texto",
            "current_message_id": 10,
            "inbound_count": 0,
            "template_version": 1,
            "repost_pending": False,
            "last_post_at": object(),
            "cycle_target_messages": 3,
            "cycle_min_interval_seconds": 900,
            "plan_expired": False,
        }
        actions = []

        class Context:
            def __init__(self, value): self.value = value
            async def __aenter__(self): return self.value
            async def __aexit__(self, *_args): return False

        class Connection:
            def transaction(self): return Context(self)
            async def fetchrow(self, *_args): return dict(state)
            async def fetchval(self, *_args): return True
            async def execute(self, query, *args):
                if "SET inbound_count=$2" in query:
                    state["inbound_count"] = int(args[1])
                elif "repost_pending=TRUE" in query:
                    state["inbound_count"] = 0
                    state["repost_pending"] = True
                elif "INSERT INTO outbox_actions" in query:
                    actions.append(args)

        connection = Connection()
        module, _pool = self.module()
        module.pool.acquire = lambda: Context(connection)
        module.dynamic_chat_ids.add(-1001)
        for message_id in range(1, 4):
            await module._count_and_queue_repost(-1001, message_id)
        self.assertEqual(len(actions), 1)
        self.assertTrue(state["repost_pending"])
        self.assertEqual(state["inbound_count"], 0)

    async def test_expired_plan_is_recalculated_but_keeps_existing_count(self):
        state = {
            "template_text": "meu texto",
            "current_message_id": 10,
            "inbound_count": 5,
            "template_version": 1,
            "repost_pending": False,
            "last_post_at": object(),
            "cycle_target_messages": 3,
            "cycle_min_interval_seconds": 900,
            "plan_expired": True,
        }

        class Context:
            def __init__(self, value): self.value = value
            async def __aenter__(self): return self.value
            async def __aexit__(self, *_args): return False

        class Connection:
            def transaction(self): return Context(self)
            async def fetchrow(self, *_args): return dict(state)
            async def fetchval(self, *_args): return False
            async def execute(self, query, *args):
                if "SET inbound_count=$2" in query:
                    state["inbound_count"] = int(args[1])

        module, _pool = self.module()
        connection = Connection()
        module.pool.acquire = lambda: Context(connection)
        module.dynamic_chat_ids.add(-1001)
        module._record_human_activity = AsyncMock()
        module._new_repost_plan = AsyncMock(return_value=RepostPlan(10, 3600, 8.0, 7200))
        module._persist_cycle_plan = AsyncMock()
        await module._count_and_queue_repost(-1001, 99)
        module._new_repost_plan.assert_awaited_once()
        self.assertEqual(state["inbound_count"], 6)

    async def test_repost_is_deferred_when_reactive_reply_has_priority(self):
        module, pool = self.module()
        pool.fetchrow = AsyncMock(return_value={
            "template_text": "meu texto", "current_message_id": 10,
            "template_version": 3, "repost_pending": True,
            "repost_approved_at": object(),
        })
        module._reply_blocks_repost = AsyncMock(return_value=True)
        module._repost_max_defer_elapsed = AsyncMock(return_value=False)
        module._defer_repost_action = AsyncMock(return_value={"sent": False, "reason": "deferred_for_group_reply"})
        effects = SimpleNamespace(send_text=AsyncMock(), delete_messages=AsyncMock())
        result = await module.action_repost_group_text(
            {"action_key": "repost-1", "payload": {"peer": -1001, "template_version": 3}}, effects
        )
        self.assertEqual(result["reason"], "deferred_for_group_reply")
        effects.send_text.assert_not_awaited()

    async def test_max_defer_prevents_starvation_and_allows_repost(self):
        module, pool = self.module()
        pool.fetchrow = AsyncMock(return_value={
            "template_text": "meu texto", "current_message_id": 10,
            "template_version": 3, "repost_pending": True,
            "repost_approved_at": object(),
        })
        pool.fetchval = AsyncMock(return_value=3)
        module._reply_blocks_repost = AsyncMock(return_value=True)
        module._repost_max_defer_elapsed = AsyncMock(return_value=True)
        calls = []
        async def send_text(*_args):
            calls.append("send"); return {"message_id": 20}
        async def delete_messages(*_args):
            calls.append("delete"); return {"deleted": True}
        effects = SimpleNamespace(send_text=send_text, delete_messages=delete_messages)
        result = await module.action_repost_group_text(
            {"action_key": "repost-1", "payload": {"peer": -1001, "template_version": 3}}, effects
        )
        self.assertEqual(calls, ["send", "delete"])
        self.assertTrue(result["sent"])
        sql = pool.execute.call_args.args[0]
        self.assertIn("cycle_target_messages=NULL", sql)


if __name__ == "__main__":
    unittest.main()
