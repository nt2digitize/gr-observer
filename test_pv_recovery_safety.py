import asyncio
import unittest

from gr_observer.pv_editor_safety import PvEditorSafetyMixin
from gr_observer.pv_production_guard import PvProductionGuardMixin, pv_has_link


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
        self.active = 0
        self.max_active = 0

    async def accept_pv_message(self, *args, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.01)
        self.active -= 1
        return kwargs["user_id"]


class _PV:
    def __init__(self, storage):
        self.storage = storage
        self.original_step_calls = 0
        self.last_context = None

    async def _queue_sequence_continuation(self, **kwargs):
        self.last_context = kwargs["context"]
        return True

    async def action_send_message_step(self, action, effects):
        self.original_step_calls += 1
        return {"sent": True}


class _Registry:
    def __init__(self, pv):
        self.pv = pv

    def get(self, module_id):
        self.assert_id = module_id
        return type("Item", (), {"implementation": self.pv})()


class _GuardHost(PvProductionGuardMixin):
    def __init__(self):
        self.storage = _Storage()
        self.pv = _PV(self.storage)
        self.registry = _Registry(self.pv)


class ProductionGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_linkish_text_gets_preview(self):
        self.assertTrue(pv_has_link("olha aqui https://example.com/x"))
        self.assertTrue(pv_has_link("grupo t.me/exemplo"))
        self.assertTrue(pv_has_link("www.example.com/teste"))
        self.assertFalse(pv_has_link("texto sem endereço"))

    async def test_same_user_state_transition_is_serialized(self):
        host = _GuardHost()
        host._install_pv_production_guards()
        await asyncio.gather(
            host.storage.accept_pv_message(user_id=55),
            host.storage.accept_pv_message(user_id=55),
        )
        self.assertEqual(host.storage.max_active, 1)

    async def test_new_continuation_receives_state_token(self):
        host = _GuardHost()
        host._install_pv_production_guards()
        await host.pv._queue_sequence_continuation(
            peer=55,
            origin_key="x",
            block_key="greeting",
            branch_key=None,
            after_position="0",
            continuation="none",
            context={"peer": 55},
            variables={},
            prewait=1,
        )
        self.assertEqual(
            host.pv.last_context["_pv_guard"],
            {"stage": "greeting_queued", "last_inbound_message_id": 10},
        )

    async def test_legacy_continuation_is_quarantined(self):
        host = _GuardHost()
        host._install_pv_production_guards()
        result = await host.pv.action_send_message_step(
            {"payload": {"peer": 55, "context": {}}}, None
        )
        self.assertEqual(result["reason"], "legacy_step_quarantined")
        self.assertEqual(host.pv.original_step_calls, 0)

    async def test_changed_conversation_cannot_send_old_step(self):
        host = _GuardHost()
        host._install_pv_production_guards()
        action = {
            "payload": {
                "peer": 55,
                "context": {
                    "_pv_guard": {
                        "stage": "greeting_queued",
                        "last_inbound_message_id": 10,
                    }
                },
            }
        }
        host.storage.pool.message_id = 11
        result = await host.pv.action_send_message_step(action, None)
        self.assertEqual(result["reason"], "conversation_state_changed")
        self.assertEqual(host.pv.original_step_calls, 0)


class _App:
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


class _SafePanel(PvEditorSafetyMixin, _PanelBase):
    pass


class _Event:
    def __init__(self, raw_text="", data=b"pvm:home"):
        self.raw_text = raw_text
        self.data = data


class EditorSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_abandons_edit_without_mutating_copy(self):
        panel = _SafePanel()
        panel.pv_editor_pending = {"mode": "text", "step_id": 1}
        await panel.on_callback(_Event(data=b"pvm:home"))
        self.assertIsNone(panel.pv_editor_pending)
        self.assertEqual(panel.base_callbacks, 1)

    async def test_empty_media_while_editing_preserves_copy(self):
        panel = _SafePanel()
        panel.pv_editor_pending = {
            "mode": "text",
            "step_id": 1,
            "block_key": "link",
        }
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


class _AsyncNone:
    def __await__(self):
        if False:
            yield None
        return None


if __name__ == "__main__":
    unittest.main()
