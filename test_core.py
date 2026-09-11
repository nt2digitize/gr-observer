"""Offline behavioral and architecture tests; no real credentials are used."""

import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telethon import errors, types

from gr_observer.application import Observer
from gr_observer.catalog import (
    BOTSON_DETAILS,
    CAMPAIGNS,
    COMMANDS,
    LIVE_INVITE_VARIANTS,
    LIVE_REMARKETING_VARIANTS,
    MODULES,
    PV_GREETING_VARIANTS,
    match_command,
    parse_botson_detail,
    validate_catalog,
)
from gr_observer.config import Settings
from gr_observer.domain import source_label
from gr_observer.modules.botson import BotsonModule
from gr_observer.modules.botson_engine import button_policy, telegram_bot_from_url
from gr_observer.modules.pv_reply import (
    PvReplyModule,
    classify_live_response,
    classify_response,
    followup_delay_seconds,
    greeting_for,
    is_human_sender,
    variant_for,
)
from gr_observer.modules.radar import RadarModule, telegram_link_target
from gr_observer.outbox import (
    AmbiguousExternalEffect,
    OutboxWriter,
    TelegramEffects,
    stable_random_id,
)
from gr_observer.registry import ModuleRegistry
from gr_observer.schema import SCHEMA
from gr_observer.storage import run_id_for


def settings(**overrides):
    values = dict(
        api_id=1,
        api_hash="hash",
        control_bot_token="token",
        admin_id=123,
        database_url="postgresql://test",
        user_session_string="session",
        history_limit=20,
        scan_interval_minutes=360,
        pv_preview_link="https://t.me/+private-test-link",
        pv_reply_auto_enable=False,
        pv_reply_delay_seconds=60,
        pv_followup_min_hours=23.0,
        pv_followup_max_hours=25.0,
        pv_followup_max_cycles=7,
        pv_weekly_interval_hours=168.0,
        botson_previews=("preview",),
        botson_bot_targets=(),
        botson_controller_id=123,
        botson_pair_code=None,
        botson_trigger_text="testar",
        botson_max_clicks=12,
        botson_max_depth=5,
        botson_entry_wait_seconds=12.0,
        botson_recovery_wait_seconds=20.0,
        botson_report_ttl_seconds=21600,
    )
    values.update(overrides)
    return Settings(**values)


class CatalogTests(unittest.TestCase):
    def test_source_has_name_and_id(self):
        self.assertEqual(source_label("Grupo Teste", -100123), "Grupo Teste (-100123)")
        self.assertEqual(source_label(None, -100123), "-100123")

    def test_command_catalog_has_no_ambiguous_alias(self):
        validate_catalog()

    def test_original_commands_are_preserved(self):
        self.assertEqual(match_command("/ligar", "panel"), "radar.enable")
        self.assertEqual(match_command("/desligar@meubot", "panel"), "radar.disable")
        self.assertEqual(match_command("/observador", "panel"), "core.dashboard")
        self.assertEqual(match_command("/status", "panel"), "core.status")

    def test_plain_text_commands_control_modules(self):
        self.assertEqual(match_command("  LIGAR   RADAR ", "panel"), "radar.enable")
        self.assertEqual(match_command("ligar botson", "panel"), "botson.enable")
        self.assertEqual(match_command("testar botson", "panel"), "botson.run")
        self.assertEqual(
            match_command("ligar atendimento", "panel"), "pv_reply.enable"
        )

    def test_user_trigger_is_surface_scoped(self):
        self.assertEqual(match_command("testar", "user"), "botson.run")
        self.assertIsNone(match_command("ligar radar", "user"))

    def test_detail_grammar_is_dictionary_driven(self):
        parsed = parse_botson_detail("2 evidências")
        self.assertEqual((parsed.index, parsed.action), (1, "evidence"))
        self.assertEqual(parse_botson_detail("1").action, "summary")
        self.assertEqual(parse_botson_detail("3 retestar").action, "retest")
        self.assertIsNone(parse_botson_detail("um resumo"))

    def test_orders_are_explicit_and_unique(self):
        self.assertEqual(
            len({item["order"] for item in COMMANDS.values()}), len(COMMANDS)
        )
        self.assertEqual(
            len({item["order"] for item in MODULES.values()}), len(MODULES)
        )
        self.assertEqual(
            len({item["order"] for item in CAMPAIGNS.values()}), len(CAMPAIGNS)
        )
        self.assertEqual(
            len({item["order"] for item in BOTSON_DETAILS.values()}),
            len(BOTSON_DETAILS),
        )


class SettingsTests(unittest.TestCase):
    def test_radar_requires_only_shared_session_after_panel(self):
        cfg = settings(user_session_string="")
        self.assertIn("USER_SESSION_STRING", cfg.module_blocker("radar"))

    def test_botson_requires_allowlist(self):
        cfg = settings(botson_previews=())
        self.assertIn("BOTSON_PREVIEW_ALLOWLIST", cfg.module_blocker("botson"))

    def test_botson_accepts_pair_code_instead_of_controller(self):
        cfg = settings(botson_controller_id=None, botson_pair_code="code")
        self.assertIsNone(cfg.module_blocker("botson"))

    def test_pv_reply_requires_private_preview_link(self):
        cfg = settings(pv_preview_link="")
        self.assertIn("PV_PREVIEW_LINK", cfg.module_blocker("pv_reply"))


class RegistryTests(unittest.TestCase):
    def test_registry_rejects_duplicate_rib(self):
        registry = ModuleRegistry()
        registry.register("radar", object())
        with self.assertRaises(ValueError):
            registry.register("radar", object())

    def test_registry_uses_catalog_order(self):
        registry = ModuleRegistry()
        registry.register("botson", object())
        registry.register("pv_reply", object())
        registry.register("radar", object())
        self.assertEqual(
            [item.module_id for item in registry.ordered()],
            ["radar", "pv_reply", "botson"],
        )

    def test_state_is_per_module(self):
        registry = ModuleRegistry()
        registry.register("radar", object())
        registry.register("botson", object())
        registry.apply_states(
            {
                "radar": {"enabled": True, "reason": "Ligado"},
                "botson": {"enabled": False, "reason": "Pausado"},
            }
        )
        self.assertTrue(registry.get("radar").enabled)
        self.assertFalse(registry.get("botson").enabled)


class RadarTests(unittest.IsolatedAsyncioTestCase):
    def radar(self, permissions):
        pool = NS(execute=AsyncMock())
        module = RadarModule(pool, settings(), AsyncMock())
        module.client = AsyncMock()
        module.client.get_permissions.return_value = permissions
        module.me = NS(id=1)
        module.save_chat = AsyncMock()
        return module

    async def test_preview_does_not_allow_advertising(self):
        module = self.radar(
            NS(send_messages=True, send_media=True, embed_link_previews=True)
        )
        entity = NS(id=5, kind="group")
        await module.inspect_permissions(entity)
        values = module.save_chat.call_args.kwargs
        self.assertIsNone(values["can_links"])
        self.assertEqual(values["risk"], "medium")

    async def test_unknown_permissions_fail_closed(self):
        module = self.radar(NS())
        await module.inspect_permissions(NS(id=5, kind="group"))
        self.assertIsNone(module.save_chat.call_args.kwargs["can_text"])

    async def test_manual_group_post_becomes_postable(self):
        module = self.radar(NS())
        event = NS(
            out=True,
            is_group=True,
            is_channel=False,
            media=None,
            raw_text="post manual",
            get_chat=AsyncMock(return_value=NS()),
        )
        self.assertTrue(await module.record_manual_post(event))
        self.assertTrue(module.save_chat.call_args.kwargs["can_text"])
        self.assertNotIn("can_links", module.save_chat.call_args.kwargs)

    async def test_manual_link_confirms_links(self):
        module = self.radar(NS())
        event = NS(
            out=True,
            is_group=True,
            is_channel=False,
            media=None,
            raw_text="veja https://example.com",
            get_chat=AsyncMock(return_value=NS()),
        )
        await module.record_manual_post(event)
        self.assertTrue(module.save_chat.call_args.kwargs["can_links"])
        self.assertEqual(module.save_chat.call_args.kwargs["risk"], "low")

    async def test_incoming_message_does_not_mark_postable(self):
        module = self.radar(NS())
        event = NS(out=False, is_group=True, is_channel=False)
        self.assertFalse(await module.record_manual_post(event))
        module.save_chat.assert_not_awaited()

    def test_telegram_links_are_classified_without_joining(self):
        self.assertEqual(
            telegram_link_target("https://t.me/+Abc_123"),
            ("invite", "Abc_123"),
        )
        self.assertEqual(
            telegram_link_target("https://t.me/grupo_teste"),
            ("public", "grupo_teste"),
        )
        self.assertIsNone(telegram_link_target("https://example.com/grupo"))

    async def test_private_invite_preview_classifies_group_and_approval(self):
        pool = NS(
            fetchrow=AsyncMock(return_value={"url": "https://t.me/+Abc_123"}),
            execute=AsyncMock(),
        )
        module = RadarModule(pool, settings(), AsyncMock())
        module.client = AsyncMock()
        module.client.return_value = types.ChatInvite(
            title="Grupo candidato",
            photo=types.PhotoEmpty(id=0),
            participants_count=10,
            color=0,
            channel=True,
            megagroup=True,
            request_needed=True,
        )

        status = await module.audit_link(9)

        self.assertEqual(status, "not_joined")
        args = pool.execute.call_args.args
        self.assertEqual(args[4:6], ("Grupo candidato", "group"))
        self.assertTrue(args[-1])

    def test_schema_has_persistent_link_organizer(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS link_targets", SCHEMA)
        self.assertIn("membership_status", SCHEMA)
        self.assertIn("disposition", SCHEMA)

    def test_passive_module_has_no_telegram_writes(self):
        source = ast.parse(Path("gr_observer/modules/radar.py").read_text())
        forbidden_attributes = {
            "send_message",
            "send_file",
            "send_panel_text",
            "forward_messages",
            "click",
        }
        forbidden_names = {
            "JoinChannelRequest",
            "LeaveChannelRequest",
            "ImportChatInviteRequest",
        }
        for node in ast.walk(source):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                self.assertNotIn(node.func.attr, forbidden_attributes)
            if isinstance(node, ast.Name):
                self.assertNotIn(node.id, forbidden_names)

    async def test_open_group_notice_is_persisted_in_outbox(self):
        pool = NS(
            fetchrow=AsyncMock(return_value={"can_text": False}),
            fetchval=AsyncMock(return_value=False),
            execute=AsyncMock(),
        )
        module = RadarModule(pool, settings(), AsyncMock())
        module.client = AsyncMock()
        module.client.get_permissions.return_value = NS(
            send_messages=True, send_media=True
        )
        module.me = NS(id=1)
        module.save_chat = AsyncMock()

        await module.inspect_permissions(NS(id=5, title="Grupo janela", kind="group"))

        calls = [call.args[0] for call in pool.execute.await_args_list]
        self.assertTrue(any("INSERT INTO outbox_actions" in sql for sql in calls))


class CoreNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_notice_uses_effect_gateway(self):
        observer = object.__new__(Observer)
        observer.admin_id = 123
        effects = NS(send_panel_text=AsyncMock(return_value={"message_id": 7}))
        action = {
            "action_key": "radar-open:5:hour",
            "payload": {"text": "grupo abriu"},
        }

        result = await observer.action_notify_admin(action, effects)

        self.assertEqual(result["message_id"], 7)
        effects.send_panel_text.assert_awaited_once_with(
            123, "grupo abriu", "radar-open:5:hour:send"
        )


class PvReplyTests(unittest.IsolatedAsyncioTestCase):
    def module(self, **overrides):
        storage = NS(
            accept_pv_message=AsyncMock(return_value="greeting_queued"),
            pv_action_allowed=AsyncMock(return_value=True),
            pv_reminder_link_allowed=AsyncMock(return_value=True),
            mark_pv_greeting_sent=AsyncMock(return_value=True),
            mark_pv_link_sent_and_schedule=AsyncMock(return_value=True),
            complete_pv_followup_and_schedule_next=AsyncMock(return_value=True),
            complete_pv_weekly_and_schedule_next=AsyncMock(return_value=True),
            queue_live_optin=AsyncMock(),
            live_optin_allowed=AsyncMock(return_value=True),
            mark_live_optin_asked=AsyncMock(),
            live_recipient_allowed=AsyncMock(return_value=True),
            mark_live_invite_and_schedule_remarketing=AsyncMock(),
            mark_live_remarketing_sent=AsyncMock(),
            close_live_recipient=AsyncMock(),
            live_campaign_link=AsyncMock(return_value="https://example.com/live"),
            mark_live_link_delivered=AsyncMock(),
        )
        module = PvReplyModule(storage, settings(**overrides))
        module.me = NS(id=999)
        return module, storage

    def test_human_filter_excludes_bots_deleted_and_telegram_service(self):
        self.assertTrue(is_human_sender(NS(id=10, bot=False, deleted=False)))
        self.assertFalse(is_human_sender(NS(id=10, bot=True, deleted=False)))
        self.assertFalse(is_human_sender(NS(id=10, bot=False, deleted=True)))
        self.assertFalse(is_human_sender(NS(id=777000, bot=False, deleted=False)))

    def test_approved_greeting_copy_is_preserved(self):
        self.assertEqual(
            PV_GREETING_VARIANTS,
            (
                "Quer ver minha esposa puta?",
                "Quer ver a minha puta?",
                "Quer ver minha safada?",
                "Quer ver minha esposa bem safada?",
                "Quer ver minha puta aprontando?",
                "Quer ver minha esposa sem-vergonha?",
                "Tá a fim de ver minha safada?",
                "Quer ver como minha esposa é puta?",
                "Quer conhecer a minha safada?",
                "Quer ver a minha mulher bem puta?",
            ),
        )
        self.assertIn(greeting_for("same-action"), PV_GREETING_VARIANTS)
        self.assertEqual(greeting_for("same-action"), greeting_for("same-action"))

    def test_weekly_answers_and_opt_out_are_classified(self):
        self.assertEqual(classify_response("Sim, já entrei e gostei"), "positive")
        self.assertEqual(classify_response("Ainda não entrei"), "negative")
        self.assertEqual(classify_response("não gostei"), "unknown")
        self.assertEqual(classify_response("pare, não me mande mais"), "opt_out")
        self.assertEqual(classify_response("parece interessante"), "unknown")
        self.assertEqual(classify_response("uma pergunta comum"), "unknown")

    def test_live_answers_are_classified_without_changing_preview_rules(self):
        self.assertEqual(classify_live_response("quero"), "positive")
        self.assertEqual(classify_live_response("manda o link"), "positive")
        self.assertEqual(classify_live_response("não quero"), "negative")
        self.assertEqual(classify_live_response("uma pergunta comum"), "unknown")
        self.assertEqual(classify_response("quero"), "unknown")

    def test_progressive_delays_stay_inside_the_requested_windows(self):
        one = followup_delay_seconds(42, 1, 23, 25)
        two = followup_delay_seconds(42, 2, 23, 25)
        three = followup_delay_seconds(42, 3, 23, 25)
        seven = followup_delay_seconds(42, 7, 23, 25)
        self.assertTrue(23 * 3600 <= one <= 25 * 3600)
        self.assertTrue(47 * 3600 <= two <= 49 * 3600)
        self.assertTrue(71 * 3600 <= three <= 73 * 3600)
        self.assertTrue(167 * 3600 <= seven <= 169 * 3600)
        self.assertEqual(one, followup_delay_seconds(42, 1, 23, 25))

    async def test_real_private_message_is_queued_without_raw_text(self):
        module, storage = self.module()
        event = NS(
            is_private=True,
            out=False,
            chat_id=10,
            sender_id=10,
            id=7,
            raw_text="mensagem privada que não deve ser persistida",
            get_sender=AsyncMock(
                return_value=NS(
                    id=10,
                    bot=False,
                    deleted=False,
                    support=False,
                    username="pessoa",
                    first_name="Pessoa",
                    last_name="Real",
                )
            ),
        )
        self.assertFalse(await module.handle_event(event))
        kwargs = storage.accept_pv_message.call_args.kwargs
        self.assertEqual(kwargs["response_kind"], "unknown")
        self.assertEqual(kwargs["delay_seconds"], 60)
        self.assertNotIn(event.raw_text, repr(kwargs))

    async def test_bot_private_message_is_ignored(self):
        module, storage = self.module()
        event = NS(
            is_private=True,
            out=False,
            get_sender=AsyncMock(return_value=NS(id=10, bot=True)),
        )
        self.assertFalse(await module.handle_event(event))
        storage.accept_pv_message.assert_not_awaited()

    async def test_link_starts_progressive_phase_then_weekly_mode(self):
        module, storage = self.module()
        effects = NS(send_text=AsyncMock(return_value={"message_id": 8}))
        action = {
            "action_key": "pv-link",
            "payload": {"peer": 10, "campaign_id": "pv.preview_link"},
        }
        with patch("gr_observer.modules.pv_reply.asyncio.sleep", AsyncMock()) as sleep:
            result = await module.action_send_link(action, effects)
        self.assertTrue(result["sent"])
        self.assertEqual(effects.send_text.await_count, 2)
        self.assertEqual(
            effects.send_text.await_args_list[0].args[1],
            "Entra no grupo de prévias dela! Posso mandar o link",
        )
        self.assertEqual(
            effects.send_text.await_args_list[1].args[1],
            "https://t.me/+private-test-link",
        )
        sleep.assert_awaited_once_with(7)
        storage.queue_live_optin.assert_awaited_once_with(10, 20 * 60)
        call = storage.mark_pv_link_sent_and_schedule.call_args.kwargs
        self.assertTrue(23 * 3600 <= call["delay_seconds"] <= 25 * 3600)
        self.assertEqual(call["weekly_delay_seconds"], 7 * 24 * 3600)
        self.assertEqual(call["max_cycles"], 7)

    async def test_weekly_sequence_uses_separate_link_balloons(self):
        module, storage = self.module()
        effects = NS(send_text=AsyncMock(return_value={"message_id": 9}))
        action = {
            "action_key": "weekly-1",
            "payload": {"peer": 10, "campaign_id": "pv.weekly_question", "cycle": 1},
        }
        with patch("gr_observer.modules.pv_reply.asyncio.sleep", AsyncMock()) as sleep:
            result = await module.action_send_weekly_question(action, effects)
        self.assertTrue(result["sent"])
        texts = [call.args[1] for call in effects.send_text.await_args_list]
        self.assertEqual(
            texts,
            [
                "E aí, safado! Tá gozando muito?",
                "https://t.me/+private-test-link",
                (
                    "Se ainda não entrou ou se saiu, entra de novo. "
                    "Abre em duas telas e goza pra ela ver 😈"
                ),
                "https://t.me/+private-test-link",
            ],
        )
        sleep.assert_awaited_once_with(7)
        call = storage.complete_pv_weekly_and_schedule_next.call_args.kwargs
        self.assertEqual(call["delay_seconds"], 7 * 24 * 3600)

    async def test_followup_uses_text_and_link_as_separate_balloons(self):
        module, storage = self.module()
        effects = NS(send_text=AsyncMock(return_value={"message_id": 10}))
        action = {
            "action_key": "followup-1",
            "payload": {"peer": 10, "campaign_id": "pv.followup", "cycle": 1},
        }
        result = await module.action_send_followup(action, effects)
        self.assertTrue(result["sent"])
        self.assertEqual(
            [call.args[1] for call in effects.send_text.await_args_list],
            ["Gostou? Já gozou pra ela??", "https://t.me/+private-test-link"],
        )

    async def test_live_invite_schedules_only_one_remarketing(self):
        module, storage = self.module()
        effects = NS(send_text=AsyncMock(return_value={"message_id": 11}))
        action = {
            "action_key": "live-invite-1",
            "payload": {"peer": 10, "campaign_id": 4},
        }
        result = await module.action_send_live_invite(action, effects)
        self.assertTrue(result["sent"])
        self.assertIn(effects.send_text.call_args.args[1], LIVE_INVITE_VARIANTS)
        storage.mark_live_invite_and_schedule_remarketing.assert_awaited_once_with(
            campaign_id=4, user_id=10, delay_seconds=10 * 60
        )

    async def test_live_remarketing_and_link_are_separate_actions(self):
        module, storage = self.module()
        effects = NS(send_text=AsyncMock(return_value={"message_id": 12}))
        action = {
            "action_key": "live-remarketing-1",
            "payload": {"peer": 10, "campaign_id": 4},
        }
        result = await module.action_send_live_remarketing(action, effects)
        self.assertTrue(result["sent"])
        self.assertIn(effects.send_text.call_args.args[1], LIVE_REMARKETING_VARIANTS)
        storage.mark_live_remarketing_sent.assert_awaited_once_with(4, 10, 10 * 60)

        link_action = {
            "action_key": "live-link-1",
            "payload": {"peer": 10, "campaign_id": 4},
        }
        await module.action_send_live_link(link_action, effects)
        self.assertEqual(effects.send_text.call_args.args[1], "https://example.com/live")
        storage.mark_live_link_delivered.assert_awaited_once_with(4, 10)


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    def test_stable_random_id_is_repeatable_signed_int64(self):
        first = stable_random_id("same-effect")
        self.assertEqual(first, stable_random_id("same-effect"))
        self.assertNotEqual(first, stable_random_id("other-effect"))
        self.assertTrue(-(2**63) <= first < 2**63)
        self.assertNotEqual(first, 0)

    def test_run_id_is_deterministic(self):
        self.assertEqual(run_id_for("event"), run_id_for("event"))
        self.assertNotEqual(run_id_for("event"), run_id_for("other"))

    async def test_succeeded_effect_is_not_executed_again(self):
        storage = NS(
            begin_effect=AsyncMock(return_value=("succeeded", {"message_id": 7})),
            finish_effect=AsyncMock(),
            review_effect=AsyncMock(),
        )
        effects = TelegramEffects(storage, NS(), 1)
        operation = AsyncMock()
        result = await effects.perform("key", "send", {}, operation)
        self.assertEqual(result["message_id"], 7)
        operation.assert_not_awaited()

    async def test_ambiguous_effect_is_never_blindly_repeated(self):
        storage = NS(begin_effect=AsyncMock(return_value=("ambiguous", None)))
        effects = TelegramEffects(storage, NS(), 1)
        operation = AsyncMock()
        with self.assertRaises(AmbiguousExternalEffect):
            await effects.perform("key", "click", {}, operation)
        operation.assert_not_awaited()

    async def test_reconciled_effect_is_closed_without_repeating(self):
        storage = NS(
            begin_effect=AsyncMock(return_value=("ambiguous", None)),
            finish_effect=AsyncMock(),
        )
        effects = TelegramEffects(storage, NS(), 1)
        operation = AsyncMock()
        reconcile = AsyncMock(return_value={"status": "ok"})
        result = await effects.perform("key", "join", {}, operation, reconcile)
        self.assertEqual(result, {"status": "ok"})
        storage.finish_effect.assert_awaited_once()
        operation.assert_not_awaited()

    async def test_operation_error_moves_effect_to_review(self):
        storage = NS(
            begin_effect=AsyncMock(return_value=("execute", None)),
            finish_effect=AsyncMock(),
            review_effect=AsyncMock(),
        )
        effects = TelegramEffects(storage, NS(), 1)
        operation = AsyncMock(side_effect=TimeoutError("ambiguous"))
        with self.assertRaises(AmbiguousExternalEffect):
            await effects.perform("key", "send", {}, operation)
        storage.review_effect.assert_awaited_once()
        storage.finish_effect.assert_not_awaited()

    async def test_writer_refuses_disabled_module(self):
        actions = [
            {
                "id": 1,
                "action_key": "a",
                "module_id": "botson",
                "action_type": "reply",
                "payload": {},
            },
            None,
        ]
        storage = NS(
            claim_next_action=AsyncMock(side_effect=actions),
            fail_action=AsyncMock(),
            finish_action=AsyncMock(),
        )
        writer = OutboxWriter(storage, NS(), module_enabled=lambda _module: False)
        writer.register("botson", "reply", AsyncMock())

        async def stop_after_failure(*_args, **_kwargs):
            writer.stop()

        storage.fail_action.side_effect = stop_after_failure
        await writer.run()
        storage.fail_action.assert_awaited_once()
        writer.handlers[("botson", "reply")].assert_not_awaited()


class BotsonSafetyTests(unittest.TestCase):
    def test_payment_button_is_blocked(self):
        allowed, _ = button_policy({"text": "Pagar com PIX", "data": b"x", "url": None})
        self.assertFalse(allowed)

    def test_safe_navigation_callback_is_allowed(self):
        allowed, _ = button_policy(
            {"text": "Ver conteúdo", "data": "open", "url": None}
        )
        self.assertTrue(allowed)

    def test_hidden_payment_callback_is_blocked(self):
        allowed, _ = button_policy(
            {"text": "Continuar", "data": "payment:pix", "url": None}
        )
        self.assertFalse(allowed)

    def test_external_url_is_not_opened(self):
        allowed, _ = button_policy(
            {"text": "Abrir site", "data": None, "url": "https://example.com"}
        )
        self.assertFalse(allowed)

    def test_only_telegram_bot_links_are_discovered(self):
        self.assertEqual(telegram_bot_from_url("https://t.me/TesteBot"), "TesteBot")
        self.assertIsNone(telegram_bot_from_url("https://t.me/canal"))


class BotsonCommandTests(unittest.IsolatedAsyncioTestCase):
    def module(self, **setting_overrides):
        storage = NS(
            accept_and_enqueue=AsyncMock(return_value="accepted"),
            enqueue_action=AsyncMock(),
            pair_and_enqueue=AsyncMock(return_value="accepted"),
        )
        module = BotsonModule(storage, settings(**setting_overrides))
        return module, storage

    async def test_custom_legacy_trigger_is_preserved(self):
        module, storage = self.module(botson_trigger_text="rodar agora")
        event = NS(
            is_private=True,
            out=False,
            raw_text="RODAR AGORA",
            chat_id=123,
            sender_id=123,
            id=44,
        )
        self.assertTrue(await module.handle_event(event))
        self.assertEqual(
            storage.accept_and_enqueue.call_args.kwargs["action_type"], "run_fleet"
        )

    async def test_unknown_sender_cannot_run_test(self):
        module, storage = self.module()
        event = NS(
            is_private=True,
            out=False,
            raw_text="testar",
            chat_id=999,
            sender_id=999,
            id=45,
        )
        self.assertFalse(await module.handle_event(event))
        storage.accept_and_enqueue.assert_not_awaited()

    async def test_pair_code_is_not_written_to_event_payload(self):
        module, storage = self.module(
            botson_controller_id=None, botson_pair_code="segredo"
        )
        event = NS(
            is_private=True,
            out=False,
            raw_text="ativar botson segredo",
            chat_id=456,
            sender_id=456,
            id=46,
        )
        self.assertTrue(await module.handle_event(event))
        sent = repr(storage.pair_and_enqueue.call_args.kwargs)
        self.assertNotIn("segredo", sent)
        self.assertEqual(module.controller_id, 456)


class ApplicationCoreTests(unittest.IsolatedAsyncioTestCase):
    def bare_observer(self):
        observer = Observer.__new__(Observer)
        observer.pool = NS()
        return observer

    async def test_explicit_first_deploy_opt_in_enables_pv_only_once(self):
        observer = self.bare_observer()
        observer.settings = settings(pv_reply_auto_enable=True)
        observer.storage = NS(set_module_state=AsyncMock())
        observer.registry = ModuleRegistry()
        observer.registry.register("pv_reply", object())
        item = observer.registry.get("pv_reply")
        item.reason = item.spec["initial_reason"]

        await observer.apply_startup_requests()

        self.assertTrue(item.enabled)
        observer.storage.set_module_state.assert_awaited_once()

        item.enabled = False
        item.reason = "Pausado pelo administrador"
        observer.storage.set_module_state.reset_mock()
        await observer.apply_startup_requests()
        self.assertFalse(item.enabled)
        observer.storage.set_module_state.assert_not_awaited()

    async def test_user_session_lock_is_held_until_release(self):
        observer = self.bare_observer()
        conn = NS(fetchval=AsyncMock(return_value=True), execute=AsyncMock())
        observer.pool = NS(acquire=AsyncMock(return_value=conn), release=AsyncMock())
        held = await observer.acquire_user_session_lock()
        self.assertIs(held, conn)
        observer.pool.release.assert_not_awaited()
        await observer.release_user_session_lock(held)
        conn.execute.assert_awaited_once_with(
            "SELECT pg_advisory_unlock($1)", observer.USER_SESSION_LOCK_KEY
        )
        observer.pool.release.assert_awaited_once_with(conn)

    async def test_overlapping_deploy_waits_for_handoff(self):
        observer = self.bare_observer()
        conn = NS(fetchval=AsyncMock(return_value=False), execute=AsyncMock())
        observer.pool = NS(acquire=AsyncMock(return_value=conn), release=AsyncMock())
        with patch.object(asyncio, "sleep", new=AsyncMock()) as sleep:
            await observer.acquire_user_session_lock()
        conn.execute.assert_awaited_once_with(
            "SELECT pg_advisory_lock($1)", observer.USER_SESSION_LOCK_KEY
        )
        sleep.assert_awaited_once_with(observer.USER_SESSION_HANDOFF_SECONDS)

    async def test_failed_lock_acquisition_releases_connection(self):
        observer = self.bare_observer()
        conn = NS(fetchval=AsyncMock(side_effect=RuntimeError("db")))
        observer.pool = NS(acquire=AsyncMock(return_value=conn), release=AsyncMock())
        with self.assertRaises(RuntimeError):
            await observer.acquire_user_session_lock()
        observer.pool.release.assert_awaited_once_with(conn)

    def test_auth_key_error_requires_new_session(self):
        reason = Observer.observer_failure_reason(
            errors.AuthKeyDuplicatedError(request=None)
        )
        self.assertIn("Sessão invalidada", reason)
        self.assertIn("nova USER_SESSION_STRING", reason)

    def test_schema_contains_dual_write_guards(self):
        for table in (
            "inbox_events",
            "outbox_actions",
            "telegram_effects",
            "module_runs",
            "pv_reply_contacts",
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", SCHEMA)
        self.assertIn("available_at TIMESTAMPTZ", SCHEMA)

    async def test_operational_command_does_not_leak_into_radar(self):
        observer = self.bare_observer()
        botson_impl = NS(handle_event=AsyncMock(return_value=True))
        radar_impl = NS(handle_event=AsyncMock())
        observer.registry = ModuleRegistry()
        observer.registry.register("radar", radar_impl)
        observer.registry.register("botson", botson_impl)
        observer.registry.get("radar").enabled = True
        observer.registry.get("botson").enabled = True
        observer.connected_modules = {"radar", "botson"}
        observer.active_events = set()

        await observer.guarded_dispatch(NS())

        botson_impl.handle_event.assert_awaited_once()
        radar_impl.handle_event.assert_not_awaited()

    async def test_private_business_event_reaches_pv_and_radar_in_order(self):
        observer = self.bare_observer()
        calls = []

        async def botson(_event):
            calls.append("botson")
            return False

        async def pv_reply(_event):
            calls.append("pv_reply")
            return False

        async def radar(_event):
            calls.append("radar")
            return False

        observer.registry = ModuleRegistry()
        observer.registry.register("radar", NS(handle_event=radar))
        observer.registry.register("pv_reply", NS(handle_event=pv_reply))
        observer.registry.register("botson", NS(handle_event=botson))
        for item in observer.registry.ordered():
            item.enabled = True
        observer.connected_modules = {"radar", "pv_reply", "botson"}
        observer.active_events = set()

        await observer.guarded_dispatch(NS())

        self.assertEqual(calls, ["botson", "pv_reply", "radar"])


if __name__ == "__main__":
    unittest.main()
