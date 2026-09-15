import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import functions, types

from gr_observer.contact_ledger import ContactLedger
from gr_observer.group_contact_flow import (
    GroupContactFlow,
    _stable_delay,
    classify_add_intent,
    mentions_me,
)
from gr_observer.modules.group_reply_contacts import GroupReplyWithContacts
from gr_observer.modules.pv_reply_contacts import PvReplyWithContacts


class ContactClassifierTests(unittest.TestCase):
    def test_add_intents_are_narrow_and_normalized(self):
        for text in (
            "add",
            "me adiciona lá",
            "tô de ban",
            "estou de ban",
            "não consigo chamar",
            "salva aí",
        ):
            with self.subTest(text=text):
                self.assertTrue(classify_add_intent(text))
        self.assertFalse(classify_add_intent("boa noite pessoal"))

    def test_mention_is_explicit(self):
        self.assertTrue(mentions_me("@RadarTeste add", "RadarTeste"))
        self.assertFalse(mentions_me("radarteste add", "RadarTeste"))
        self.assertFalse(mentions_me("@outro add", "RadarTeste"))

    def test_add_delay_is_stable_between_25_and_35_seconds(self):
        first = _stable_delay("group-add:-100:77:42")
        second = _stable_delay("group-add:-100:77:42")
        self.assertEqual(first, second)
        self.assertGreaterEqual(first, 25)
        self.assertLessEqual(first, 35)


class FeatureFlagTests(unittest.TestCase):
    def test_group_contact_features_are_off_by_default(self):
        with patch.dict(os.environ, {}, clear=True):
            flow = GroupContactFlow(SimpleNamespace())
        self.assertFalse(flow.add_enabled)
        self.assertFalse(flow.protection_enabled)
        self.assertEqual(flow.protection_seconds, 24 * 3600)

    def test_pv_auto_save_is_off_by_default(self):
        pool = SimpleNamespace()
        storage = SimpleNamespace(pool=pool)
        settings = SimpleNamespace()
        with patch.dict(os.environ, {}, clear=True):
            module = PvReplyWithContacts(storage, settings)
        self.assertFalse(module.auto_save_contacts)


class ContactLedgerEffectTests(unittest.IsolatedAsyncioTestCase):
    async def test_add_contact_uses_user_effect_without_phone_sharing(self):
        pool = SimpleNamespace(execute=AsyncMock(), fetchval=AsyncMock(return_value="save_queued"))
        ledger = ContactLedger(pool)
        ledger.account_user_id = 999

        class FakeClient:
            def __init__(self): self.request = None
            async def get_input_entity(self, _peer):
                return types.InputPeerUser(user_id=42, access_hash=123456)
            async def __call__(self, request):
                self.request = request
                return SimpleNamespace()

        client = FakeClient()
        before_write = AsyncMock()

        class FakeEffects:
            def __init__(self):
                self.client = client
                self.before_user_write = before_write
            async def perform(self, _key, _effect_type, _payload, operation):
                return await operation()

        result = await ledger.save_with_effects({
            "action_key": "contact-save:999:42:test",
            "payload": {"peer": 42, "account_user_id": 999, "username": "teste",
                        "display_name": "Pessoa Teste", "first_name": "Pessoa", "last_name": "Teste"},
        }, FakeEffects())
        self.assertTrue(result["saved"])
        self.assertIsInstance(client.request, functions.contacts.AddContactRequest)
        self.assertEqual(client.request.phone, "")
        self.assertFalse(client.request.add_phone_privacy_exception)
        before_write.assert_awaited_once()


class KillSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def test_pv_pending_contact_action_is_noop_when_disabled(self):
        storage = SimpleNamespace(pool=SimpleNamespace())
        module = PvReplyWithContacts(storage, SimpleNamespace())
        module.auto_save_contacts = False
        module.contact_ledger.save_with_effects = AsyncMock()
        result = await module.action_ensure_contact_saved({"payload": {"peer": 42}}, SimpleNamespace())
        self.assertEqual(result["reason"], "feature_disabled")
        module.contact_ledger.save_with_effects.assert_not_awaited()

    async def test_group_pending_add_action_is_noop_when_disabled(self):
        pool = SimpleNamespace()
        storage = SimpleNamespace(pool=pool)
        module = GroupReplyWithContacts(storage, SimpleNamespace())
        module.contact_flow.add_enabled = False
        module.contact_flow.action_add_contact_reply = AsyncMock()
        result = await module.action_group_add_contact_reply({"payload": {"peer": -100}}, SimpleNamespace())
        self.assertEqual(result["reason"], "feature_disabled")
        module.contact_flow.action_add_contact_reply.assert_not_awaited()


class ProtectionAttributionTests(unittest.IsolatedAsyncioTestCase):
    async def test_bare_direct_mention_protects_current_loop_post(self):
        flow = GroupContactFlow(SimpleNamespace())
        flow.me = SimpleNamespace(id=999, username="RadarTeste")
        flow.add_enabled = False
        flow.protection_enabled = True
        flow._reply_context = AsyncMock(return_value=(False, None))
        flow._current_repost_message = AsyncMock(return_value=10)
        flow._protect_message = AsyncMock()
        event = SimpleNamespace(chat_id=-1001, raw_text="@RadarTeste você é do RJ?", id=77)
        sender = SimpleNamespace(id=42)
        await flow.observe_event(event, sender, module_id="group_reply")
        flow._current_repost_message.assert_awaited_once_with(-1001)
        flow._protect_message.assert_awaited_once_with(
            chat_id=-1001,
            message_id=10,
            reason="mention",
            event_id=77,
            module_id="group_reply",
        )

    async def test_unrelated_mention_does_not_protect_current_loop_post(self):
        flow = GroupContactFlow(SimpleNamespace())
        flow.me = SimpleNamespace(id=999, username="RadarTeste")
        flow.add_enabled = False
        flow.protection_enabled = True
        flow._reply_context = AsyncMock(return_value=(False, None))
        flow._current_repost_message = AsyncMock(return_value=10)
        flow._protect_message = AsyncMock()
        event = SimpleNamespace(chat_id=-1001, raw_text="@OutraPessoa você é do RJ?", id=78)
        sender = SimpleNamespace(id=42)
        await flow.observe_event(event, sender, module_id="group_reply")
        flow._current_repost_message.assert_not_awaited()
        flow._protect_message.assert_not_awaited()


class ProtectedRepostTests(unittest.IsolatedAsyncioTestCase):
    async def test_repost_deletes_old_copy_even_when_engagement_is_protected(self):
        pool = SimpleNamespace(
            fetchrow=AsyncMock(return_value={"template_text": "meu texto", "current_message_id": 10,
                                             "template_version": 3, "repost_pending": True,
                                             "repost_approved_at": None}),
            fetchval=AsyncMock(return_value=3), execute=AsyncMock(),
        )
        module = GroupReplyWithContacts(SimpleNamespace(pool=pool), SimpleNamespace())
        module._reply_blocks_repost = AsyncMock(return_value=False)
        module.contact_flow.is_message_protected = AsyncMock(return_value=True)
        module.contact_flow.clear_protection = AsyncMock()
        module._mark_automated_outbound = lambda *_args: None
        effects = SimpleNamespace(send_text=AsyncMock(return_value={"message_id": 20}),
                                  delete_messages=AsyncMock(return_value={"deleted": True}))
        result = await module.action_repost_group_text(
            {"action_key": "repost-1", "payload": {"peer": -1001, "template_version": 3}}, effects)
        self.assertTrue(result["sent"])
        self.assertTrue(result["deleted"])
        effects.delete_messages.assert_awaited_once_with(-1001, [10], "repost-1:delete-old")
        module.contact_flow.is_message_protected.assert_not_awaited()
        module.contact_flow.clear_protection.assert_awaited_once_with(-1001, 10)
        pool.execute.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
