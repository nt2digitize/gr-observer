"""Offline behavioral tests; real Telegram integration requires credentials."""
import ast
import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

source = ast.parse(Path(__file__).with_name('app.py').read_text())
class FloodWaitError(Exception):
    seconds = 60

namespace = dict(asyncio=asyncio, os=os, errors=NS(FloodWaitError=FloodWaitError),
                 log=logging.getLogger('test'), kind_of=lambda e: e.kind,
                 now=lambda: None, title_of=lambda e: e.title,
                 utils=NS(get_peer_id=lambda e: e.peer_id),
                 functions=NS(channels=NS(GetFullChannelRequest=lambda e: e)))
cls = next(n for n in source.body if isinstance(n, ast.ClassDef) and n.name == 'Observer')
exec(compile(ast.Module(body=[cls], type_ignores=[]), 'app.py', 'exec'), namespace)
Observer = namespace['Observer']

class CoreTests(unittest.IsolatedAsyncioTestCase):
    def observer(self, permissions):
        obj = Observer.__new__(Observer)
        obj.user = AsyncMock()
        obj.user.get_permissions.return_value = permissions
        obj.user.return_value = NS(full_chat=NS(slowmode_seconds=30))
        obj.save_chat = AsyncMock()
        obj.me = NS(id=1)
        return obj

    async def test_preview_does_not_allow_advertising(self):
        obj = self.observer(NS(send_messages=True, send_media=True, embed_link_previews=True))
        await obj.inspect_permissions(NS(id=5, kind='group'))
        values = obj.save_chat.call_args.kwargs
        self.assertIsNone(values['can_links'])
        self.assertEqual(values['risk'], 'medium')

    async def test_unknown_permissions_fail_closed(self):
        obj = self.observer(NS())
        await obj.inspect_permissions(NS(id=5, kind='group'))
        self.assertIsNone(obj.save_chat.call_args.kwargs['can_text'])

    async def test_readonly_channel(self):
        obj = self.observer(NS())
        await obj.inspect_permissions(NS(id=5, kind='channel', creator=False))
        self.assertFalse(obj.save_chat.call_args.kwargs['can_text'])

    async def test_floodwait_is_not_swallowed(self):
        obj = self.observer(NS())
        obj.user.get_permissions.side_effect = FloodWaitError()
        with self.assertRaises(FloodWaitError):
            await obj.inspect_permissions(NS(id=5, kind='group'))

    async def test_metadata_floodwait_is_not_swallowed(self):
        obj = self.observer(NS())
        obj.user.side_effect = FloodWaitError()
        with self.assertRaises(FloodWaitError):
            await obj.inspect_permissions(NS(id=5, kind='group'))

    async def test_event_floodwait_signals_stop(self):
        obj = Observer.__new__(Observer)
        obj.enabled = True
        obj.active_events = set()
        obj.pause_tasks = set()
        obj.set_enabled = AsyncMock()
        obj.observe_event = AsyncMock(side_effect=FloodWaitError())
        await obj.guarded_observe(NS())
        await asyncio.gather(*list(obj.pause_tasks))
        self.assertFalse(obj.enabled)
        obj.set_enabled.assert_awaited_once()
        self.assertFalse(obj.set_enabled.call_args.args[0])

    async def test_inventory_uses_same_signed_id_as_events(self):
        obj = Observer.__new__(Observer)
        obj.pool = NS(execute=AsyncMock())
        await obj.save_chat(NS(id=5, peer_id=-1000000000005, title='test', kind='group'))
        self.assertEqual(obj.pool.execute.call_args.args[1], -1000000000005)

    def control_observer(self):
        obj = Observer.__new__(Observer)
        obj.admin_id = 123
        obj.enabled = False
        obj.worker = None
        obj.active_events = set()
        obj.control_lock = asyncio.Lock()
        obj.pool = NS(execute=AsyncMock())
        obj.panel = NS(disconnect=AsyncMock())
        obj.state_reason = 'Pausado'
        return obj

    async def test_paused_events_do_not_read_telegram(self):
        obj = self.control_observer()
        obj.observe_event = AsyncMock()
        await obj.guarded_observe(NS())
        obj.observe_event.assert_not_awaited()

    async def test_only_admin_private_commands(self):
        obj = self.control_observer()
        obj.set_enabled = AsyncMock()
        for sender_id, private in [(999, True), (123, False)]:
            event = NS(sender_id=sender_id, is_private=private, raw_text='/ligar', respond=AsyncMock())
            await obj.control_command(event)
            event.respond.assert_not_awaited()
        obj.set_enabled.assert_not_awaited()

    async def test_admin_can_pause(self):
        obj = self.control_observer()
        event = NS(sender_id=123, is_private=True, raw_text='/desligar', respond=AsyncMock())
        await obj.control_command(event)
        self.assertFalse(obj.pool.execute.call_args.args[1])
        event.respond.assert_awaited_once()
        obj.panel.disconnect.assert_not_awaited()

    async def test_missing_session_keeps_panel_available(self):
        obj = self.control_observer()
        with patch.dict(os.environ, {'USER_SESSION_STRING': ''}):
            response = await obj.set_enabled(True)
        self.assertIn('USER_SESSION_STRING', response)
        self.assertIsNone(obj.worker)
        self.assertFalse(obj.pool.execute.call_args.args[1])
        obj.panel.disconnect.assert_not_awaited()

    async def test_on_is_idempotent_and_off_cancels_work(self):
        obj = self.control_observer()
        async def waiting_worker():
            await asyncio.Event().wait()
        obj.observer_worker = waiting_worker
        with patch.dict(os.environ, {'USER_SESSION_STRING': 'test-only'}):
            await obj.set_enabled(True)
            first = obj.worker
            self.assertTrue(obj.pool.execute.call_args.args[1])
            await obj.set_enabled(True)
            self.assertIs(obj.worker, first)
            await obj.set_enabled(False)
        self.assertTrue(first.cancelled())
        self.assertFalse(obj.pool.execute.call_args.args[1])
        obj.panel.disconnect.assert_not_awaited()

    def test_observer_has_no_message_sending_calls(self):
        forbidden = {'send_message', 'send_file', 'forward_messages', 'click', 'JoinChannelRequest', 'ImportChatInviteRequest'}
        for node in ast.walk(source):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden)

if __name__ == '__main__':
    unittest.main()
