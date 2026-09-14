import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from telethon import errors

from gr_observer.application import Observer
from gr_observer.registry import ModuleRegistry


class DispatchIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_channel_private_error_is_local_to_one_rib(self):
        observer = Observer.__new__(Observer)
        observer.registry = ModuleRegistry()
        observer.active_events = set()
        observer.pause_module = AsyncMock()
        calls = []

        async def botson(_event):
            calls.append("botson")
            return False

        async def group_reply(_event):
            calls.append("group_reply")
            raise errors.ChannelPrivateError(request=None)

        async def radar(_event):
            calls.append("radar")
            return False

        observer.registry.register("radar", NS(handle_event=radar))
        observer.registry.register("group_reply", NS(handle_event=group_reply))
        observer.registry.register("botson", NS(handle_event=botson))
        for item in observer.registry.ordered():
            item.enabled = True
        observer.connected_modules = {"radar", "group_reply", "botson"}

        await observer.guarded_dispatch(NS())

        self.assertEqual(calls, ["botson", "group_reply", "radar"])
        observer.pause_module.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
