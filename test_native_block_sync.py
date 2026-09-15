from types import SimpleNamespace
import inspect
import unittest
from unittest.mock import AsyncMock, patch

from telethon import events, types
from telethon.tl.functions.contacts import GetBlockedRequest

from gr_observer.modules.pv_reply_production import PvReplyProduction


class FakeBlockedClient:
    def __init__(self, result):
        self.result = result
        self.requests = []

    async def __call__(self, request):
        self.requests.append(request)
        return self.result


class NativeBlockSyncTests(unittest.IsolatedAsyncioTestCase):
    def build_module(self):
        module = object.__new__(PvReplyProduction)
        module.storage = SimpleNamespace(pool=object())
        module.me = SimpleNamespace(id=999)
        return module

    async def test_main_block_event_uses_existing_kill_switch(self):
        module = self.build_module()
        update = SimpleNamespace(
            blocked=True,
            blocked_my_stories_from=False,
            peer_id=types.PeerUser(123456),
        )
        with patch(
            "gr_observer.modules.pv_reply_production.suppress_pv_user",
            new_callable=AsyncMock,
        ) as suppress:
            suppress.return_value = 4
            await module._on_native_block_update(update)
        suppress.assert_awaited_once_with(
            module.storage.pool,
            123456,
            suppressed_by=999,
            reason="telegram_native_block",
        )

    async def test_unblock_does_not_resume_automation(self):
        module = self.build_module()
        update = SimpleNamespace(
            blocked=False,
            blocked_my_stories_from=False,
            peer_id=types.PeerUser(123456),
        )
        with patch(
            "gr_observer.modules.pv_reply_production.suppress_pv_user",
            new_callable=AsyncMock,
        ) as suppress:
            await module._on_native_block_update(update)
        suppress.assert_not_awaited()

    async def test_story_only_block_is_not_pv_suppression(self):
        module = self.build_module()
        update = SimpleNamespace(
            blocked=True,
            blocked_my_stories_from=True,
            peer_id=types.PeerUser(123456),
        )
        with patch(
            "gr_observer.modules.pv_reply_production.suppress_pv_user",
            new_callable=AsyncMock,
        ) as suppress:
            await module._on_native_block_update(update)
        suppress.assert_not_awaited()

    async def test_startup_reconcile_imports_preexisting_blocked_users(self):
        module = self.build_module()
        result = SimpleNamespace(
            blocked=[
                SimpleNamespace(peer_id=types.PeerUser(111)),
                SimpleNamespace(peer_id=types.PeerChat(222)),
                SimpleNamespace(user_id=333),
            ]
        )
        client = FakeBlockedClient(result)
        with patch(
            "gr_observer.modules.pv_reply_production.suppress_pv_user",
            new_callable=AsyncMock,
        ) as suppress:
            suppress.return_value = 0
            count = await module._reconcile_native_blocklist(client, 999)
        self.assertEqual(count, 2)
        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertIsInstance(request, GetBlockedRequest)
        self.assertEqual(request.offset, 0)
        self.assertEqual(request.limit, 100)
        self.assertEqual(suppress.await_count, 2)

    def test_handler_is_raw_update_on_same_pv_client(self):
        source = inspect.getsource(PvReplyProduction.on_connect)
        self.assertIn("events.Raw(types.UpdatePeerBlocked)", source)
        self.assertIn("client.add_event_handler", source)
        self.assertNotIn("TelegramClient(", source)
        self.assertNotIn("StringSession(", source)

    def test_disconnect_removes_native_block_handler(self):
        source = inspect.getsource(PvReplyProduction.on_disconnect)
        self.assertIn("remove_event_handler", source)
        self.assertIn("_native_block_handler", source)


if __name__ == "__main__":
    unittest.main()
