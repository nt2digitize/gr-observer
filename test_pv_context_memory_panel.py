"""Regression contract for Contexts and Memory in shadow mode."""

import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.pv_response_memory import PvResponseMemoryShadow

ROOT = Path(__file__).resolve().parent


class PvMemoryReadModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_disabled_snapshot_does_no_io(self):
        pool = NS(execute=AsyncMock(), fetch=AsyncMock())
        memory = PvResponseMemoryShadow(pool, enabled=False)
        snapshot = await memory.operator_snapshot()
        self.assertFalse(snapshot["enabled"])
        pool.execute.assert_not_awaited()
        pool.fetch.assert_not_awaited()

    async def test_enabled_but_not_ready_snapshot_does_no_io(self):
        pool = NS(execute=AsyncMock(), fetch=AsyncMock())
        memory = PvResponseMemoryShadow(pool, enabled=True)
        snapshot = await memory.operator_snapshot()
        self.assertTrue(snapshot["enabled"])
        self.assertFalse(snapshot["ready"])
        pool.execute.assert_not_awaited()
        pool.fetch.assert_not_awaited()

    async def test_ready_snapshot_is_bounded_and_read_only(self):
        pool = NS(execute=AsyncMock(), fetch=AsyncMock())
        memory = PvResponseMemoryShadow(pool, enabled=True)
        memory._ready = True
        pool.fetch.side_effect = [
            [{"intent": "price", "contacts": 3, "last_seen": None}],
            [{"intent": "price", "response_text": "resposta humana", "times_seen": 2, "last_seen_at": None}],
        ]
        snapshot = await memory.operator_snapshot(limit=99)
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["contexts"][0]["contacts"], 3)
        self.assertEqual(snapshot["responses"][0]["times_seen"], 2)
        self.assertEqual(pool.fetch.await_args_list[1].args[1], 20)
        pool.execute.assert_not_awaited()


class PvMemoryPanelContractTests(unittest.TestCase):
    def test_panel_reads_existing_shadow_without_activation(self):
        source = (ROOT / "gr_observer" / "group_integration.py").read_text(encoding="utf-8")
        read_method = source.split("async def _pv_memory_snapshot", 1)[1].split(
            "async def _show_pv_conversation_info", 1
        )[0]
        self.assertIn("response_memory_shadow", read_method)
        self.assertIn("operator_snapshot", read_method)
        self.assertNotIn("ensure_schema", read_method)
        self.assertNotIn("set_module_enabled", read_method)

    def test_memory_panel_keeps_automation_off(self):
        source = (ROOT / "gr_observer" / "group_integration.py").read_text(encoding="utf-8")
        panel = source.split("async def _show_pv_conversation_info", 1)[1].split(
            "@staticmethod", 1
        )[0]
        self.assertIn("Somente observa/classifica", panel)
        self.assertIn("ainda não aprovados para automação", panel)
        self.assertIn("AUTOMAÇÃO — OFF", panel)
        self.assertNotIn("writer.register", panel)
        self.assertNotIn("outbox_actions", panel)


if __name__ == "__main__":
    unittest.main()
