"""Offline behavioral tests; real Telegram integration requires credentials."""
import ast
import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

source = ast.parse(Path(__file__).with_name('app.py').read_text())
class FloodWaitError(Exception):
    seconds = 60

namespace = dict(asyncio=asyncio, errors=NS(FloodWaitError=FloodWaitError),
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
        obj.stop_event = asyncio.Event()
        obj.observe_event = AsyncMock(side_effect=FloodWaitError())
        await obj.guarded_observe(NS())
        self.assertTrue(obj.stop_event.is_set())

    async def test_inventory_uses_same_signed_id_as_events(self):
        obj = Observer.__new__(Observer)
        obj.pool = NS(execute=AsyncMock())
        await obj.save_chat(NS(id=5, peer_id=-1000000000005, title='test', kind='group'))
        self.assertEqual(obj.pool.execute.call_args.args[1], -1000000000005)

    def test_observer_has_no_message_sending_calls(self):
        forbidden = {'send_message', 'send_file', 'forward_messages', 'click', 'JoinChannelRequest', 'ImportChatInviteRequest'}
        for node in ast.walk(source):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden)

if __name__ == '__main__':
    unittest.main()
