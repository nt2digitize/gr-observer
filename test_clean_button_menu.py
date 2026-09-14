"""Regression tests for the compact operator button menu."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class CleanButtonMenuTests(unittest.TestCase):
    def setUp(self):
        self.menu = (ROOT / "gr_observer" / "clean_menu_panel.py").read_text(
            encoding="utf-8"
        )
        self.integration = (ROOT / "gr_observer" / "group_integration.py").read_text(
            encoding="utf-8"
        )
        self.catalog = (ROOT / "gr_observer" / "catalog.py").read_text(
            encoding="utf-8"
        )

    def test_compact_menu_is_composed_before_existing_panel_mixins(self):
        self.assertIn("CleanMenuPanelMixin", self.integration)
        self.assertIn(
            "CleanMenuPanelMixin,\n    PvMessageEditorPanelMixin,\n    BaseControlPanel",
            self.integration,
        )

    def test_home_exposes_six_operator_areas_and_status(self):
        for callback in (
            'b"menu:radar"',
            'b"menu:pv"',
            'b"menu:groups"',
            'b"menu:inventory"',
            'b"menu:organizer"',
            'b"menu:system"',
            'b"menu:status"',
        ):
            self.assertIn(callback, self.menu)

    def test_daily_areas_have_contextual_controls(self):
        self.assertIn('b"command:pv_preview"', self.menu)
        self.assertIn('b"pv:two_screens"', self.menu)
        self.assertIn('b"live:new"', self.menu)
        self.assertIn('b"menu:groups:preview"', self.menu)
        self.assertIn('b"list:posting_active:0"', self.menu)
        self.assertIn('b"organizer"', self.menu)

    def test_all_operational_modules_can_be_toggled_from_buttons(self):
        for module_id in ("radar", "pv_reply", "group_reply", "botson"):
            self.assertIn(module_id, self.menu)
        self.assertIn("menu:toggle:", self.menu)

    def test_text_commands_are_preserved_as_secondary_shortcuts(self):
        for trigger in (
            '"/start"',
            '"/status"',
            '"/funcoes"',
            '"/ligar"',
            '"/desligar"',
            '"/ligar_atendimento"',
            '"/desligar_atendimento"',
            '"/mensagens_pv"',
            '"/ligar_grupos"',
            '"/desligar_grupos"',
            '"/atendimento_grupos"',
            '"/testar_botson"',
        ):
            self.assertIn(trigger, self.catalog)
        self.assertIn("Atalhos de texto", self.menu)

    def test_menu_adds_no_second_client_writer_or_background_worker(self):
        self.assertNotIn("TelegramClient(", self.menu)
        self.assertNotIn("OutboxWriter(", self.menu)
        self.assertNotIn("create_task(", self.menu)


if __name__ == "__main__":
    unittest.main()
