"""Regression tests for nonzero human reading + typing timing."""

import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.human_timing import (
    MIN_WRITING_DELAY_SECONDS,
    minimum_human_delay_seconds,
    reading_seconds,
    split_human_delay,
    typing_seconds,
)
from gr_observer.modules.pv_reply_contacts import (
    HUMAN_TIMING_MIGRATION_VERSION,
    PvReplyWithContacts,
)

ROOT = Path(__file__).resolve().parent


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class HumanTimingMathTests(unittest.TestCase):
    def test_no_user_facing_text_has_zero_minimum(self):
        for text in ("oi", "vem comigo", "uma frase um pouco maior para responder alguém"):
            self.assertGreaterEqual(
                minimum_human_delay_seconds(text, "same-key"),
                MIN_WRITING_DELAY_SECONDS,
            )

    def test_longer_text_needs_more_human_time(self):
        short = "oi"
        long = "essa é uma mensagem bem maior com várias palavras para leitura e digitação"
        self.assertGreaterEqual(
            minimum_human_delay_seconds(long, "k"),
            minimum_human_delay_seconds(short, "k"),
        )
        self.assertGreaterEqual(typing_seconds(long, "k"), typing_seconds(short, "k"))
        self.assertGreaterEqual(reading_seconds(long, "k"), reading_seconds(short, "k"))

    def test_retry_is_stable(self):
        text = "mensagem estável para retry"
        self.assertEqual(typing_seconds(text, "retry"), typing_seconds(text, "retry"))
        self.assertEqual(reading_seconds(text, "retry"), reading_seconds(text, "retry"))
        self.assertEqual(
            minimum_human_delay_seconds(text, "retry"),
            minimum_human_delay_seconds(text, "retry"),
        )

    def test_split_never_collapses_to_zero(self):
        prewait, typing = split_human_delay(0, "texto automático", "zero")
        self.assertGreater(typing, 0)
        self.assertGreater(prewait + typing, 0)
        self.assertGreaterEqual(prewait + typing, MIN_WRITING_DELAY_SECONDS)


class HumanTimingMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_zero_medians_are_raised_once(self):
        conn = NS(
            transaction=lambda: _AsyncContext(None),
            fetchval=AsyncMock(return_value=None),
            execute=AsyncMock(),
        )
        pool = NS(acquire=lambda: _AsyncContext(conn))
        module = object.__new__(PvReplyWithContacts)
        module.storage = NS(pool=pool)

        await module._ensure_human_timing_floor()

        self.assertEqual(conn.execute.await_count, 2)
        update_sql = conn.execute.await_args_list[0].args[0]
        self.assertIn("median_delay_seconds<$1", update_sql)
        self.assertEqual(conn.execute.await_args_list[0].args[1], MIN_WRITING_DELAY_SECONDS)
        self.assertEqual(
            conn.execute.await_args_list[1].args[1], HUMAN_TIMING_MIGRATION_VERSION
        )

    async def test_migration_is_not_reapplied(self):
        conn = NS(
            transaction=lambda: _AsyncContext(None),
            fetchval=AsyncMock(return_value=1),
            execute=AsyncMock(),
        )
        pool = NS(acquire=lambda: _AsyncContext(conn))
        module = object.__new__(PvReplyWithContacts)
        module.storage = NS(pool=pool)

        await module._ensure_human_timing_floor()

        conn.execute.assert_not_awaited()


class IntegrationContractTests(unittest.TestCase):
    def test_pv_uses_same_step_key_for_schedule_and_send(self):
        source = (ROOT / "gr_observer" / "modules" / "pv_reply_contacts.py").read_text(
            encoding="utf-8"
        )
        # Both scheduling and sending route through the same helper. The helper
        # derives exactly one deterministic :step:<id> key from its caller key.
        self.assertIn("def _human_plan", source)
        self.assertIn('timing_key = f"{origin_key}:step:{row[\'id\']}"', source)
        self.assertIn("self._human_plan(\n            row, stable_key, rendered", source)
        self.assertIn("self._human_plan(row, origin_key, text)", source)
        self.assertNotIn(":first:", source)

    def test_editor_rejects_zero(self):
        source = (ROOT / "gr_observer" / "pv_message_panel.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("Zero não é permitido", source)
        self.assertIn("seconds < minimum", source)

    def test_group_human_writes_show_typing(self):
        source = (
            ROOT / "gr_observer" / "modules" / "group_reply_contacts.py"
        ).read_text(encoding="utf-8")
        self.assertIn("SetTypingRequest", source)
        self.assertIn("action_send_group_reply", source)
        self.assertIn("action_group_add_contact_reply", source)
        self.assertIn("action_repost_group_text", source)


if __name__ == "__main__":
    unittest.main()
