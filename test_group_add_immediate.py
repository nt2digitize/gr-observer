import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gr_observer.group_contact_flow import GroupContactFlow


class GroupAddImmediateTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_add_is_enqueued_without_pre_delay(self):
        pool = SimpleNamespace(execute=AsyncMock())
        flow = GroupContactFlow(pool)
        flow.add_enabled = True
        flow.protection_enabled = False
        flow.me = SimpleNamespace(id=999, username="RadarTeste")
        flow._reply_context = AsyncMock(return_value=(True, 10))
        flow.contacts.observe = AsyncMock()
        flow.contacts.mark_already_saved = AsyncMock()

        event = SimpleNamespace(chat_id=-1001, raw_text="add", id=77)
        sender = SimpleNamespace(
            id=42,
            username="teste",
            first_name="Pessoa",
            last_name="Teste",
            contact=False,
        )

        await flow.observe_event(event, sender, module_id="group_reply")

        pool.execute.assert_awaited_once()
        args = pool.execute.await_args.args
        self.assertEqual(args[-1], 0)
        self.assertIn("group_add_contact_reply", args[0])


if __name__ == "__main__":
    unittest.main()
