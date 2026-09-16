"""Regression contract for the Conversation Free operator surface."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class PvConversationMenuTests(unittest.TestCase):
    def test_conversation_menu_exposes_expected_sections(self):
        source = (ROOT / "gr_observer" / "group_integration.py").read_text(encoding="utf-8")
        section = source.split('"conversation": (', 1)[1].split('"config": (', 1)[0]
        for label in (
            "Contextos e Memória",
            "Base de fatos",
            "Respostas observadas",
            "Respostas aprovadas",
            "Listener",
            "Automação — OFF",
        ):
            self.assertIn(label, section)

    def test_conversation_surface_has_no_activation_path(self):
        source = (ROOT / "gr_observer" / "group_integration.py").read_text(encoding="utf-8")
        info = source.split("async def _show_pv_conversation_info", 1)[1].split("@staticmethod", 1)[0]
        self.assertIn("Resposta automática da Conversa Livre permanece desligada", info)
        self.assertNotIn("set_module_enabled", info)
        self.assertNotIn("writer.register", info)
        self.assertNotIn("outbox_actions", info)

    def test_conversation_layer_adds_no_schema_or_runtime(self):
        source = (ROOT / "gr_observer" / "group_integration.py").read_text(encoding="utf-8")
        self.assertNotIn("CREATE TABLE", source)
        self.assertNotIn("TelegramClient(", source)
        self.assertNotIn("OutboxWriter(", source)


if __name__ == "__main__":
    unittest.main()
