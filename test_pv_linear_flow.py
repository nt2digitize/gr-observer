import inspect
import os
import unittest
from unittest.mock import patch

from gr_observer.modules.pv_reply_production import PvReplyProduction
from gr_observer.pv_linear_flow import (
    BLOCK_TO_PHASE,
    LINEAR_SCHEMA,
    PHASE_DEFAULTS,
    PvLinearRuntimeMixin,
    linear_flow_enabled,
)


class PvLinearArchitectureTests(unittest.TestCase):
    def test_schema_is_additive_and_keeps_existing_balloon_table(self):
        upper = LINEAR_SCHEMA.upper()
        self.assertIn("ALTER TABLE PV_MESSAGE_STEPS", upper)
        self.assertIn("PV_CONVERSATION_PHASES", upper)
        self.assertIn("PV_LINEAR_SESSIONS", upper)
        self.assertIn("WAIT_FOR_REPLY", upper)
        self.assertIn("LINEAR_POSITION", upper)
        self.assertNotIn("DROP ", upper)
        self.assertNotIn("TRUNCATE ", upper)
        self.assertNotIn("DELETE FROM", upper)

    def test_frames_are_context_only_and_old_reminder_is_not_seeded(self):
        self.assertEqual(
            [phase[0] for phase in PHASE_DEFAULTS],
            ["entry", "preview", "warmup", "remarketing"],
        )
        self.assertEqual(BLOCK_TO_PHASE["greeting"], "entry")
        self.assertEqual(BLOCK_TO_PHASE["link"], "preview")
        self.assertEqual(BLOCK_TO_PHASE["followup"], "warmup")
        self.assertEqual(BLOCK_TO_PHASE["weekly"], "remarketing")
        self.assertNotIn("reminder_link", BLOCK_TO_PHASE)

    def test_production_composes_linear_engine_statically(self):
        self.assertIn(PvLinearRuntimeMixin, PvReplyProduction.__mro__)

    def test_engine_adds_no_second_client_writer_or_background_worker(self):
        source = inspect.getsource(PvLinearRuntimeMixin)
        forbidden = (
            "TelegramClient(",
            "SafeOutboxWriter(",
            "create_task(",
            "StringSession(",
        )
        for token in forbidden:
            self.assertNotIn(token, source)
        self.assertIn("INSERT INTO outbox_actions", source)
        self.assertIn("send_linear_balloon", source)

    def test_linear_flow_does_not_classify_or_suppress_by_message_text(self):
        source = inspect.getsource(PvLinearRuntimeMixin)
        self.assertNotIn("suppress_pv_user", source)
        self.assertNotIn("lead_opt_out", source)
        self.assertNotIn("não me mande", source)
        self.assertNotIn("nao me mande", source)

    def test_wait_for_reply_is_per_contact_persistent_state(self):
        source = inspect.getsource(PvLinearRuntimeMixin)
        self.assertIn("status='waiting_reply'", source)
        self.assertIn("str(session[\"status\"]) != \"waiting_reply\"", source)
        self.assertIn("current_step_id", source)
        self.assertIn("current_position", source)

    def test_feature_switch_is_off_by_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PV_LINEAR_FLOW_ENABLED", None)
            self.assertFalse(linear_flow_enabled())
        with patch.dict(os.environ, {"PV_LINEAR_FLOW_ENABLED": "true"}):
            self.assertTrue(linear_flow_enabled())

    def test_linear_mode_quarantines_legacy_sequencer(self):
        source = inspect.getsource(PvReplyProduction)
        self.assertIn("legacy_flow_disabled", source)
        self.assertIn("async def _run_block", source)
        for action in (
            "action_auto_queue_two_screens_photo",
            "action_send_two_screens_photo",
            "action_close_live_recipient",
            "action_send_message_step",
        ):
            self.assertIn(f"async def {action}", source)

    def test_linear_branch_never_restores_removed_reminder_link(self):
        source = inspect.getsource(PvReplyProduction._restore_missing_required_destinations)
        self.assertNotIn("reminder.link", source)
        self.assertNotIn("reminder_link", source)


class PvLinearPanelContractTests(unittest.TestCase):
    def test_operator_controls_match_single_line_model(self):
        from gr_observer import pv_linear_panel

        source = inspect.getsource(pv_linear_panel.PvLinearPanelMixin)
        for text in (
            "CONVERSA PV — LINHA ÚNICA",
            "Esperar antes de enviar",
            "AGUARDAR RESPOSTA",
            "CONTINUAR AUTOMATICAMENTE",
            "⬆️ Subir",
            "⬇️ Descer",
            "➕ Balão depois",
            "🗂 Quadro",
            "➕ Novo quadro",
        ):
            self.assertIn(text, source)

    def test_operator_can_catalogue_common_telegram_media(self):
        from gr_observer import pv_linear_panel

        source = inspect.getsource(pv_linear_panel.PvLinearPanelMixin._linear_media_kind)
        for kind in ("gif", "video", "photo", "sticker", "voice", "audio", "document"):
            self.assertIn(kind, source)


if __name__ == "__main__":
    unittest.main()
