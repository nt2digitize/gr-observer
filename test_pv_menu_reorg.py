"""Regression contract for the visual-only PV menu reorganization."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class PvMenuReorgTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "gr_observer" / "group_integration.py").read_text(
            encoding="utf-8"
        )

    def test_top_level_sections_match_operator_model(self):
        expected = (
            'Button.inline("👋 Entrada", b"pvmenu:entry")',
            'Button.inline("📸 Duas telas / Homenagem", b"pvmenu:two_screens")',
            'Button.inline("⚡ Eventos", b"pvmenu:events")',
            'Button.inline("♻️ Remarketing", b"pvmenu:remarketing")',
            'Button.inline("💭 Conversa livre", b"pvmenu:conversation")',
            'Button.inline("⏸ Pausar chat de uma pessoa", b"pvstop:picker")',
            'Button.inline("⚙️ Configuração", b"pvmenu:config")',
        )
        for marker in expected:
            with self.subTest(marker=marker):
                self.assertIn(marker, self.source)

    def test_existing_actions_are_reused_not_replaced(self):
        for callback in (
            'b"command:pv_preview"',
            'b"pv:two_screens"',
            'b"live:new"',
            'b"origins"',
            'b"drafts"',
            'b"pvstop:picker"',
        ):
            with self.subTest(callback=callback):
                self.assertIn(callback, self.source)

    def test_visual_layer_adds_no_runtime_or_worker(self):
        for forbidden in (
            "TelegramClient(",
            "OutboxWriter(",
            "create_task(",
            "USER_SESSION_STRING",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, self.source)

    def test_conversation_area_does_not_enable_automation(self):
        section = self.source.split('"conversation": (', 1)[1].split('"config": (', 1)[0]
        self.assertIn("nada novo responde automaticamente", section)
        self.assertNotIn("send_text(", section)
        self.assertNotIn("enqueue", section)


if __name__ == "__main__":
    unittest.main()
