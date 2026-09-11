"""Offline behavioral and architecture tests; no real credentials are used."""

import ast
import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telethon import errors

from gr_observer.application import Observer
from gr_observer.catalog import (
    BOTSON_DETAILS,
    COMMANDS,
    MODULES,
    match_command,
    parse_botson_detail,
    validate_catalog,
)
from gr_observer.config import Settings
from gr_observer.domain import source_label
from gr_observer.modules.botson import BotsonModule
from gr_observer.modules.botson_engine import button_policy, telegram_bot_from_url
from gr_observer.modules.radar import RadarModule
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


class RegistryTests(unittest.TestCase):
    def test_registry_rejects_duplicate_rib(self):
        registry = ModuleRegistry()
        registry.register("radar", object())
        with self.assertRaises(ValueError):
            registry.register("radar", object())

    def test_registry_uses_catalog_order(self):
        registry = ModuleRegistry()
        registry.register("botson", object())
        registry.register("radar", object())
        self.assertEqual(
            [item.module_id for item in registry.ordered()], ["radar", "botson"]
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

    def test_passive_module_has_no_telegram_writes(self):
        source = ast.parse(Path("gr_observer/modules/radar.py").read_text())
        forbidden_attributes = {
            "send_message",
            "send_file",
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
        ):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", SCHEMA)

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


if __name__ == "__main__":
    unittest.main()
