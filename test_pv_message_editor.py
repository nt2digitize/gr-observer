"""Offline regression tests for the configurable PV message layer."""

import asyncio
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.catalog import (
    CAMPAIGNS,
    DIALOGS,
    LIVE_INVITE_VARIANTS,
    LIVE_REMARKETING_VARIANTS,
    PV_GREETING_VARIANTS,
)
from gr_observer.pv_message_runtime import PvMessageRuntimeMixin
from gr_observer.pv_message_steps import (
    POSITION_GAP,
    PvMessageStepStore,
    _baseline,
    has_link,
    human_jitter_bounds,
    infer_kind,
    pre_send_wait_seconds,
    stable_delay_seconds,
    typing_seconds,
)

ROOT = Path(__file__).resolve().parent


def cfg():
    return NS(
        pv_reply_delay_seconds=60,
        pv_followup_min_hours=23.0,
        pv_followup_max_hours=25.0,
        pv_weekly_interval_hours=168.0,
        pv_preview_link="https://t.me/+preview",
    )


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class MessageCatalogMigrationTests(unittest.TestCase):
    def test_seed_contains_every_current_pv_copy_family(self):
        rows = _baseline(cfg())
        contents = [row["content"] for row in rows]
        for text in PV_GREETING_VARIANTS:
            self.assertIn(text, contents)
        for text in LIVE_INVITE_VARIANTS:
            self.assertIn(text, contents)
        for text in LIVE_REMARKETING_VARIANTS:
            self.assertIn(text, contents)
        for key in (
            "pv.link_invite",
            "pv.followup",
            "pv.weekly_question",
            "pv.weekly_reentry",
            "pv.live_optin",
            "pv.two_screens.prompt",
            "pv.two_screens.question",
            "pv.two_screens.limit",
            "pv.two_screens.followup",
            "pv.two_screens.retry",
        ):
            self.assertIn(CAMPAIGNS[key]["text"], contents)
        self.assertGreaterEqual(
            contents.count(DIALOGS["pv.two_screens.preference"]), 3
        )
        self.assertIn("{preview_link}", contents)
        self.assertIn("{live_link}", contents)

    def test_seed_preserves_known_baseline_delays(self):
        by_key = {row["step_key"]: row for row in _baseline(cfg())}
        self.assertEqual(by_key["link.preview"]["median"], 7)
        self.assertEqual(by_key["live.optin"]["median"], 20 * 60)
        self.assertEqual(by_key["weekly.question"]["median"], 168 * 3600)
        self.assertEqual(by_key["followup.text"]["median"], 24 * 3600)
        self.assertEqual(by_key["two.prompt.preference"]["median"], 3)


class TimingTests(unittest.TestCase):
    def test_jitter_is_retry_stable_centered_and_bounded(self):
        for median in (5, 60, 120, 3600, 86400, 604800):
            low, high = human_jitter_bounds(median)
            first = stable_delay_seconds("same-key", median)
            second = stable_delay_seconds("same-key", median)
            self.assertEqual(first, second)
            self.assertLessEqual(low, first)
            self.assertLessEqual(first, high)
            self.assertEqual((low + high) / 2, median)

    def test_jitter_is_scale_aware_not_one_percentage(self):
        short = human_jitter_bounds(60)
        long = human_jitter_bounds(86400)
        short_fraction = (short[1] - 60) / 60
        long_fraction = (long[1] - 86400) / 86400
        self.assertNotEqual(short_fraction, long_fraction)
        self.assertLess(long_fraction, short_fraction)

    def test_typing_is_retry_stable_and_inside_total_interval(self):
        text = "Uma frase humana de tamanho razoável para o teste."
        self.assertEqual(typing_seconds(text, "k"), typing_seconds(text, "k"))
        wait, typing = pre_send_wait_seconds(60, text, "k")
        self.assertAlmostEqual(wait + typing, 60.0)
        self.assertLessEqual(typing, 8.0)


class StoreCrudTests(unittest.IsolatedAsyncioTestCase):
    async def test_edit_text_reinfers_link_kind(self):
        pool = NS(fetchval=AsyncMock(return_value=7))
        store = PvMessageStepStore(pool, cfg())
        store._ready = True
        outcome = await store.set_content(7, "https://example.com/card")
        self.assertEqual(outcome, "updated")
        args = pool.fetchval.await_args.args
        self.assertEqual(args[1], 7)
        self.assertEqual(args[2], "https://example.com/card")
        self.assertEqual(args[3], "link")

    async def test_edit_median_persists_seconds(self):
        pool = NS(fetchval=AsyncMock(return_value=9))
        store = PvMessageStepStore(pool, cfg())
        store._ready = True
        self.assertTrue(await store.set_delay(9, 120))
        self.assertEqual(pool.fetchval.await_args.args[2], 120)

    async def test_add_after_uses_position_between_neighbors_and_unique_key(self):
        parent = {
            "id": 4,
            "block_key": "link",
            "branch_key": None,
            "position": POSITION_GAP,
        }
        created = {"id": 8}
        conn = NS(
            transaction=lambda: _AsyncContext(None),
            fetchrow=AsyncMock(side_effect=[parent, created]),
            fetchval=AsyncMock(return_value=POSITION_GAP * 2),
        )
        pool = NS(acquire=lambda: _AsyncContext(conn))
        store = PvMessageStepStore(pool, cfg())
        store._ready = True
        result = await store.add_after(4, "nova fala", 120)
        self.assertEqual(result, created)
        insert_args = conn.fetchrow.await_args_list[1].args
        self.assertTrue(str(insert_args[1]).startswith("custom."))
        self.assertEqual(insert_args[4], Decimal("1500000"))
        self.assertEqual(insert_args[6], "nova fala")
        self.assertEqual(insert_args[7], "text")
        self.assertEqual(insert_args[8], 120)

    async def test_empty_content_physically_deletes_only_selected_speech(self):
        row = {
            "id": 4,
            "variant_root": True,
            "block_key": "greeting",
            "branch_key": "greeting:v1",
        }
        conn = NS(
            transaction=lambda: _AsyncContext(None),
            fetchrow=AsyncMock(return_value=row),
            fetchval=AsyncMock(return_value=5),
            execute=AsyncMock(),
        )
        pool = NS(acquire=lambda: _AsyncContext(conn))
        store = PvMessageStepStore(pool, cfg())
        store._ready = True
        self.assertEqual(await store.set_content(4, "   "), "deleted")
        delete_sql = conn.fetchrow.await_args.args[0]
        self.assertIn("DELETE FROM pv_message_steps WHERE id=$1", delete_sql)
        self.assertNotIn("branch_key=$2", delete_sql)
        conn.execute.assert_awaited_once()
        self.assertIn("variant_root=TRUE", conn.execute.await_args.args[0])


class ContentTests(unittest.TestCase):
    def test_url_is_inferred_as_link(self):
        self.assertEqual(infer_kind("https://example.com/x"), "link")
        self.assertEqual(infer_kind("{preview_link}"), "link")
        self.assertTrue(has_link("veja https://example.com/x"))
        self.assertEqual(infer_kind("texto normal"), "text")

    def test_runtime_selects_native_preview_for_link(self):
        class Dummy(PvMessageRuntimeMixin):
            pass

        dummy = Dummy()
        dummy.settings = cfg()
        dummy.message_store = NS(render=lambda content, **_: content)
        effects = NS(
            send_text=AsyncMock(return_value={"message_id": 1}),
            send_text_preview=AsyncMock(return_value={"message_id": 2}),
        )
        row = {
            "id": 7,
            "content": "https://example.com/card",
            "kind": "link",
            "median_delay_seconds": 0,
        }

        async def run():
            await dummy._send_row(
                effects=effects,
                peer=123,
                row=row,
                origin_key="origin",
                variables={},
            )

        asyncio.run(run())
        effects.send_text_preview.assert_awaited_once()
        effects.send_text.assert_not_awaited()


class PendingDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def test_deleted_pending_speech_finishes_without_review(self):
        class Dummy(PvMessageRuntimeMixin):
            pass

        dummy = Dummy()
        dummy.message_store = NS(next_step=AsyncMock(return_value=None))
        dummy._complete_sequence = AsyncMock(return_value={"advanced": True})
        action = {
            "action_key": "pv_reply:message-step:test",
            "payload": {
                "peer": 42,
                "origin_key": "origin",
                "block_key": "link",
                "branch_key": None,
                "after_position": "1000000",
                "continuation": "link",
                "context": {"peer": 42},
                "variables": {},
            },
        }
        result = await dummy.action_send_message_step(action, NS())
        self.assertTrue(result["advanced"])
        dummy._complete_sequence.assert_awaited_once()


class ArchitectureTests(unittest.TestCase):
    def test_no_second_session_writer_or_client_added(self):
        combined = "\n".join(
            (ROOT / "gr_observer" / name).read_text(encoding="utf-8")
            for name in (
                "pv_message_steps.py",
                "pv_message_runtime.py",
                "pv_message_panel.py",
            )
        )
        self.assertNotIn("TelegramClient(", combined)
        self.assertNotIn("OutboxWriter(", combined)
        self.assertNotIn("create_task(", combined)

    def test_long_waits_are_materialized_not_slept(self):
        source = (ROOT / "gr_observer" / "pv_message_runtime.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("available_at", source)
        self.assertIn("send_message_step", source)
        self.assertEqual(source.count("await asyncio.sleep("), 1)
        self.assertIn("SetTypingRequest", source)
        self.assertIn("before_user_write", source)

    def test_panel_selects_one_speech_then_shows_shared_controls(self):
        source = (ROOT / "gr_observer" / "pv_message_panel.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("FALA SELECIONADA", source)
        self.assertIn('Button.inline("✏️ Texto"', source)
        self.assertIn('Button.inline("⏱ Tempo"', source)
        self.assertIn('Button.inline("🎯 Intenção"', source)
        self.assertIn('Button.inline("⚙️ Sequência"', source)
        self.assertIn("SUBSTITUI esta fala; não cria outra", source)
        self.assertIn("pv_message_step_ui_meta", source)
        self.assertIn("self.client.edit_message", source)
        self.assertNotIn("Salvar vazio / apagar", source)

    def test_delete_is_physical_and_seed_is_versioned_once(self):
        source = (ROOT / "gr_observer" / "pv_message_steps.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("DELETE FROM pv_message_steps", source)
        self.assertIn("pv_message_step_migrations", source)
        self.assertNotIn("deleted_at", source)

    def test_existing_contact_wrapper_keeps_single_pv_rib(self):
        source = (
            ROOT / "gr_observer" / "modules" / "pv_reply_contacts.py"
        ).read_text(encoding="utf-8")
        self.assertIn("PvMessageRuntimeMixin, PvReplyModule", source)
        self.assertEqual(source.count("class PvReplyWithContacts"), 1)


if __name__ == "__main__":
    unittest.main()
