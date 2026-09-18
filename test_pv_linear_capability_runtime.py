import inspect
import unittest

from gr_observer.modules.pv_reply import PvReplyModule
from gr_observer.modules.pv_reply_production import PvReplyProduction
from gr_observer.pv_linear_capability_runtime import PvLinearCapabilityRuntimeMixin
from gr_observer.pv_linear_flow import PvLinearRuntimeMixin


class LinearCapabilityRuntimeContractTests(unittest.TestCase):
    def test_capability_layer_precedes_linear_progression_owner(self):
        mro = PvReplyProduction.__mro__
        self.assertLess(
            mro.index(PvLinearCapabilityRuntimeMixin),
            mro.index(PvLinearRuntimeMixin),
        )

    def test_runtime_adds_no_second_client_writer_worker_or_direct_telegram(self):
        source = inspect.getsource(PvLinearCapabilityRuntimeMixin)
        for token in (
            "TelegramClient(",
            "SafeOutboxWriter(",
            "OutboxWriter(",
            "create_task(",
            ".send_message(",
            ".send_file(",
        ):
            self.assertNotIn(token, source)

    def test_global_opt_out_still_runs_before_capability_dispatch(self):
        source = inspect.getsource(PvReplyProduction.handle_event)
        self.assertLess(source.index("_linear_opt_out"), source.index("super().handle_event"))

    def test_waiting_linear_capability_has_priority_over_async_live_reply(self):
        source = inspect.getsource(PvLinearCapabilityRuntimeMixin.handle_event)
        self.assertLess(
            source.index("waiting_key == TWO_SCREENS_CHOICE_STEP_KEY"),
            source.index("handle_active_live_response"),
        )
        self.assertLess(
            source.index("waiting_key == LIVE_OPTIN_STEP_KEY"),
            source.index("handle_active_live_response"),
        )

    def test_two_screens_photo_is_forced_final_then_line_resumes(self):
        source = inspect.getsource(PvLinearCapabilityRuntimeMixin.action_send_two_screens_photo)
        self.assertIn('payload["final"] = True', source)
        self.assertIn("PvReplyModule.action_send_two_screens_photo", source)
        self.assertIn("_resume_linear_capability", source)

    def test_restart_reconciles_choice_fallback_and_live_optin(self):
        source = inspect.getsource(PvLinearCapabilityRuntimeMixin._reconcile_linear_capabilities)
        self.assertIn("waiting_capabilities", source)
        self.assertIn("_open_or_repair_two_screens_wait", source)
        self.assertIn("mark_live_optin_asked", source)
        repair = inspect.getsource(PvLinearCapabilityRuntimeMixin._open_or_repair_two_screens_wait)
        self.assertIn("ensure_two_screens_fallback", repair)
        self.assertIn("photo_queued", repair)

    def test_linear_resume_is_compare_and_set_and_queues_existing_linear_action(self):
        source = inspect.getsource(PvLinearCapabilityRuntimeMixin._resume_linear_capability)
        self.assertIn("status='waiting_reply'", source)
        self.assertIn("current_step_id=$4", source)
        self.assertIn("RETURNING generation", source)
        self.assertIn("_queue_linear_step", source)

    def test_live_campaign_reuses_existing_module_actions(self):
        for name in (
            "action_send_live_invite",
            "action_send_live_remarketing",
            "action_send_live_link",
            "action_close_live_recipient",
        ):
            source = inspect.getsource(getattr(PvLinearCapabilityRuntimeMixin, name))
            self.assertIn("linear_flow_enabled()", source)
            self.assertIn(f"PvReplyModule.{name}", source)
            self.assertIn(f"return await super().{name}(action, effects)", source)
        self.assertTrue(hasattr(PvReplyModule, "action_send_live_link"))

    def test_flag_off_delegates_photo_and_live_paths(self):
        for name in (
            "action_auto_queue_two_screens_photo",
            "action_send_two_screens_photo",
            "action_send_live_optin",
            "action_send_live_invite",
            "action_send_live_remarketing",
            "action_send_live_link",
            "action_close_live_recipient",
        ):
            source = inspect.getsource(getattr(PvLinearCapabilityRuntimeMixin, name))
            self.assertIn("linear_flow_enabled()", source, name)
            self.assertIn("return await super().", source, name)


if __name__ == "__main__":
    unittest.main()
