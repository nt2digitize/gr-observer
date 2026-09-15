"""Safety tests for the passive PV MiniLearn shadow."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.pv_response_memory import (
    MINILEARN_SCHEMA,
    PvResponseMemoryShadow,
    classify_minilearn_intent,
)


class MiniLearnIntentTests(unittest.TestCase):
    def test_price_variants_collapse_without_persisting_prompt(self):
        for text in ("quanto custa?", "qual o valor do vip?", "qnt tá?", "preço?"):
            with self.subTest(text=text):
                self.assertEqual(classify_minilearn_intent(text), "price")

    def test_small_catalog_is_explicit(self):
        self.assertEqual(classify_minilearn_intent("aceita pix?"), "payment")
        self.assertEqual(classify_minilearn_intent("manda o link"), "access")
        self.assertEqual(classify_minilearn_intent("tem live?"), "live")
        self.assertEqual(classify_minilearn_intent("tem prévia?"), "preview")
        self.assertEqual(classify_minilearn_intent("tem conteúdo novo?"), "new_content")
        self.assertEqual(classify_minilearn_intent("fala comigo"), "unknown")

    def test_schema_has_no_inbound_text_column(self):
        context_ddl = MINILEARN_SCHEMA.split("CREATE TABLE IF NOT EXISTS pv_minilearn_memory", 1)[0]
        self.assertNotIn("raw_text", context_ddl)
        self.assertNotIn("message_text", context_ddl)
        self.assertNotIn("inbound_text", context_ddl)
        self.assertIn("intent TEXT NOT NULL", context_ddl)


class MiniLearnShadowTests(unittest.IsolatedAsyncioTestCase):
    def memory(self, *, enabled=True):
        pool = NS(
            execute=AsyncMock(),
            fetchrow=AsyncMock(),
            fetchval=AsyncMock(),
        )
        return PvResponseMemoryShadow(pool, enabled=enabled), pool

    async def test_disabled_shadow_does_no_io(self):
        memory, pool = self.memory(enabled=False)
        result = await memory.observe_inbound(user_id=10, message_id=1, text="quanto custa?")
        self.assertEqual(result, "disabled")
        pool.execute.assert_not_awaited()
        pool.fetchrow.assert_not_awaited()
        pool.fetchval.assert_not_awaited()

    async def test_inbound_is_reduced_to_intent_before_storage(self):
        memory, pool = self.memory()
        intent = await memory.observe_inbound(
            user_id=10,
            message_id=20,
            text="qual o valor do vip?",
        )
        self.assertEqual(intent, "price")
        sql, user_id, stored_intent, message_id = pool.execute.await_args.args
        self.assertIn("pv_minilearn_context", sql)
        self.assertEqual((user_id, stored_intent, message_id), (10, "price", 20))
        self.assertNotIn("qual o valor", sql.casefold())

    async def test_writer_effect_never_teaches_memory(self):
        memory, pool = self.memory()
        pool.fetchrow.return_value = {"intent": "price"}
        pool.fetchval.return_value = True
        result = await memory.observe_outbound(
            user_id=10,
            message_id=30,
            text="hoje tá 30",
            has_media=False,
        )
        self.assertFalse(result.learned)
        self.assertEqual(result.reason, "automation")
        writes = [call for call in pool.execute.await_args_list if "pv_minilearn_memory" in call.args[0]]
        self.assertEqual(writes, [])

    async def test_unattributed_text_reply_becomes_human_example(self):
        memory, pool = self.memory()
        pool.fetchrow.return_value = {"intent": "price"}
        pool.fetchval.return_value = False
        result = await memory.observe_outbound(
            user_id=10,
            message_id=31,
            text="hoje tá 30",
            has_media=False,
        )
        self.assertTrue(result.learned)
        self.assertEqual(result.reason, "human")
        self.assertTrue(
            any("pv_minilearn_memory" in call.args[0] for call in pool.execute.await_args_list)
        )

    async def test_unknown_context_does_not_learn(self):
        memory, pool = self.memory()
        pool.fetchrow.return_value = {"intent": "unknown"}
        result = await memory.observe_outbound(
            user_id=10,
            message_id=31,
            text="claro",
            has_media=False,
        )
        self.assertFalse(result.learned)
        self.assertEqual(result.reason, "unknown_intent")
        pool.fetchval.assert_not_awaited()

    async def test_media_is_ignored_before_database_lookup(self):
        memory, pool = self.memory()
        result = await memory.observe_outbound(
            user_id=10,
            message_id=31,
            text="foto",
            has_media=True,
        )
        self.assertFalse(result.learned)
        self.assertEqual(result.reason, "media")
        pool.fetchrow.assert_not_awaited()
        pool.fetchval.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
