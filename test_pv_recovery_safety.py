import asyncio
import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from gr_observer.modules.pv_reply_contacts import PvReplyWithContacts
from gr_observer.modules.pv_reply_production import PvReplyProduction
from gr_observer.pv_editor_policy import PvEditorPolicyMixin


class _Pool:
    def __init__(self, stage="greeting_queued", message_id=10):
        self.stage = stage
        self.message_id = message_id

    async def fetchrow(self, query, peer):
        if self.stage is None:
            return None
        return {"stage": self.stage, "last_inbound_message_id": self.message_id}


class _Storage:
    def __init__(self):
        self.pool = _Pool()


class ProductionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_same_user_events_are_serialized_by_static_class_policy(self):
        module = PvReplyProduction.__new__(PvReplyProduction)
        module._accept_locks = [asyncio.Lock() for _ in range(module._LOCK_STRIPES)]
        active = 0
        max_active = 0

        async def fake_base(_self, event):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            return False

        event = SimpleNamespace(is_private=True, out=False, sender_id=55)
        with patch.object(PvReplyWithContacts, "handle_event", fake_base):
            await asyncio.gather(module.handle_event(event), module.handle_event(event))
        self.assertEqual(max_active, 1)

    async def test_legacy_continuation_is_quarantined(self):
        module = PvReplyProduction.__new__(PvReplyProduction)
        module.storage = _Storage()
        result = await module.action_send_message_step(
            {"payload": {"peer": 55, "context": {}}}, None
        )
        self.assertEqual(result["reason"], "legacy_step_quarantined")

    async def test_changed_conversation_cannot_send_old_step(self):
        module = PvReplyProduction.__new__(PvReplyProduction)
        module.storage = _Storage()
        action = {"payload": {"peer": 55, "context": {"_pv_guard": {
            "stage": "greeting_queued", "last_inbound_message_id": 10,
        }}}}
        module.storage.pool.message_id = 11
        result = await module.action_send_message_step(action, None)
        self.assertEqual(result["reason"], "conversation_state_changed")

    def test_no_methodtype_or_runtime_assignment_exists_in_policy(self):
        source = inspect.getsource(PvReplyProduction)
        self.assertNotIn("MethodType", source)
        self.assertNotIn("= MethodType", source)
        self.assertNotIn("storage.accept_pv_message =", source)


class _App:
    settings = SimpleNamespace(pv_preview_link="https://t.me/example_preview")

    def is_admin(self, event):
        return True


class _Store:
    async def get(self, step_id):
        return {"id": step_id, "block_key": "link", "content": "{preview_link}"}


class _PanelBase:
    def __init__(self):
        self.app = _App()
        self.pv_editor_pending = None
        self.base_callbacks = 0
        self.base_actions = 0
        self.returned = False

    def _message_store(self):
        return _Store()

    async def on_callback(self, event):
        self.base_callbacks += 1

    async def on_message(self, event):
        self.base_message = True

    async def _handle_editor_action(self, event, action, step_id, *, sequence_root=None):
        self.base_actions += 1

    async def _return_view(self, event, pending, preferred_step_id=None):
        self.returned = True

    async def show_phase_index(self, event, page):
        self.returned = True

    async def _render_editor(self, event, text, buttons):
        self.rendered = text


class _SafePanel(PvEditorPolicyMixin, _PanelBase):
    pass


class _Event:
    def __init__(self, raw_text="", data=b"pvm:home"):
        self.raw_text = raw_text
        self.data = data


class EditorPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_abandons_edit_without_mutating_copy(self):
        panel = _SafePanel()
        panel.pv_editor_pending = {"mode": "text", "step_id": 1}
        await panel.on_callback(_Event(data=b"pvm:home"))
        self.assertIsNone(panel.pv_editor_pending)
        self.assertEqual(panel.base_callbacks, 1)

    async def test_empty_media_while_editing_preserves_copy(self):
        panel = _SafePanel()
        panel.pv_editor_pending = {"mode": "text", "step_id": 1, "block_key": "link"}
        await panel.on_message(_Event(raw_text=""))
        self.assertIsNone(panel.pv_editor_pending)
        self.assertTrue(panel.returned)

    async def test_legacy_empty_action_no_longer_deletes(self):
        panel = _SafePanel()
        event = _Event()
        event.answer = lambda *args, **kwargs: _AsyncNone()
        await panel._handle_editor_action(event, "empty", 1)
        self.assertEqual(panel.base_actions, 0)
        self.assertTrue(panel.returned)

    def test_editor_shows_resolved_preview_destination(self):
        panel = _SafePanel()
        self.assertEqual(
            panel._resolved_destination("{preview_link}"),
            "https://t.me/example_preview",
        )


class _AsyncNone:
    def __await__(self):
        if False:
            yield None
        return None


if __name__ == "__main__":
    unittest.main()
