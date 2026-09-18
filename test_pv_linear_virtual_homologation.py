"""Virtual homologation matrix for the complete linear PV stack.

No Telegram connection is created here. These tests lock the production contracts
that must hold before the controlled real-account homologation is authorized.
"""

import inspect
import unittest

from gr_observer.modules.pv_reply_production import PvReplyProduction
from gr_observer.outbox import TelegramEffects, action_lane
from gr_observer.pv_linear_capability_runtime import PvLinearCapabilityRuntimeMixin
from gr_observer.pv_linear_flow import PvLinearConversationStore, PvLinearRuntimeMixin


class LinearVirtualHomologationTests(unittest.TestCase):
    def test_silence_cannot_block_initial_preview_and_auto_progression_exists(self):
        gate = inspect.getsource(PvLinearConversationStore._clear_pre_preview_waits)
        self.assertIn("wait_for_reply=FALSE", gate)
        self.assertIn("linear_position <", gate)
        self.assertIn("link.preview", inspect.getsource(__import__(
            "gr_observer.pv_linear_flow", fromlist=["PREVIEW_LINK_STEP_KEY"]
        )))

        send = inspect.getsource(PvLinearRuntimeMixin.action_send_linear_balloon)
        # A balloon only pauses when its own persisted wait rule is allowed.
        self.assertIn("wait_for_reply_allowed", send)
        self.assertIn("status='waiting_reply'", send)
        # Otherwise the same lane resolves and queues its next balloon without
        # requiring any inbound human reply.
        self.assertIn("next_step", send)
        self.assertIn("_queue_linear_step", send)

    def test_reply_in_middle_does_not_skip_active_lane(self):
        accept = inspect.getsource(PvLinearRuntimeMixin._linear_accept_inbound)
        # Every inbound is remembered, but only a lane explicitly waiting for a
        # reply is allowed to advance because of that message.
        update_pos = accept.index("last_inbound_message_id=$2")
        guard_pos = accept.index('str(session["status"]) != "waiting_reply"')
        next_pos = accept.index("next_step")
        self.assertLess(update_pos, guard_pos)
        self.assertLess(guard_pos, next_pos)
        self.assertIn("return", accept[guard_pos:next_pos])

    def test_opt_out_preempts_every_linear_capability_and_stops_lane(self):
        ingress = inspect.getsource(PvReplyProduction.handle_event)
        self.assertLess(ingress.index("_linear_opt_out"), ingress.index("super().handle_event"))

        opt_out = inspect.getsource(PvReplyProduction._linear_opt_out)
        self.assertIn("suppress_pv_user", opt_out)
        self.assertIn("status='stopped'", opt_out)
        self.assertIn('!= "opt_out"', opt_out)

    def test_restart_recovers_persisted_waits_without_replaying_copy(self):
        connect = inspect.getsource(PvLinearCapabilityRuntimeMixin.on_connect)
        reconcile = inspect.getsource(
            PvLinearCapabilityRuntimeMixin._reconcile_linear_capabilities
        )
        repair = inspect.getsource(
            PvLinearCapabilityRuntimeMixin._open_or_repair_two_screens_wait
        )
        self.assertIn("_reconcile_linear_capabilities", connect)
        self.assertIn("waiting_capabilities", reconcile)
        self.assertIn("ensure_two_screens_fallback", repair)
        self.assertIn("photo_queued", repair)
        self.assertIn("mark_live_optin_asked", reconcile)
        # Restart reconciliation repairs state/actions; it does not call the
        # generic balloon sender and therefore cannot blindly resend old copy.
        self.assertNotIn("_send_row", reconcile)
        self.assertNotIn("send_linear_balloon", reconcile)

    def test_floodwait_remains_authoritative_inside_single_effect_journal(self):
        perform = inspect.getsource(TelegramEffects.perform)
        self.assertIn("except errors.FloodWaitError", perform)
        self.assertIn("wait_seconds", perform)
        self.assertIn("await asyncio.sleep(wait_seconds)", perform)
        # The effect journal is opened once before the retry loop and closed only
        # after a concrete result, so FloodWait does not create a duplicate effect.
        begin_pos = perform.index("begin_effect")
        loop_pos = perform.index("while True")
        finish_pos = perform.rindex("finish_effect")
        self.assertLess(begin_pos, loop_pos)
        self.assertLess(loop_pos, finish_pos)
        flood_block = perform[
            perform.index("except errors.FloodWaitError"):
            perform.index("except DefinitiveExternalEffectError")
        ]
        self.assertNotIn("begin_effect", flood_block)
        self.assertNotIn("finish_effect", flood_block)

    def test_two_conversations_are_distinct_logical_lanes_and_action_keys(self):
        self.assertEqual(action_lane({"module_id": "pv_reply", "payload": {"peer": 101}}), "pv:101")
        self.assertEqual(action_lane({"module_id": "pv_reply", "payload": {"peer": 202}}), "pv:202")
        self.assertNotEqual(
            action_lane({"module_id": "pv_reply", "payload": {"peer": 101}}),
            action_lane({"module_id": "pv_reply", "payload": {"peer": 202}}),
        )

        queue = inspect.getsource(PvLinearRuntimeMixin._queue_linear_step)
        self.assertIn("pv_reply:linear:{int(peer)}", queue)
        self.assertIn("ON CONFLICT(action_key) DO NOTHING", queue)
        accept = inspect.getsource(PvLinearRuntimeMixin._linear_accept_inbound)
        self.assertIn("WHERE user_id=$1", accept)
        self.assertIn("pv_linear_sessions", accept)

    def test_two_screens_and_live_return_control_to_same_linear_lane(self):
        photo = inspect.getsource(
            PvLinearCapabilityRuntimeMixin.action_send_two_screens_photo
        )
        self.assertIn('payload["final"] = True', photo)
        self.assertIn("_resume_linear_capability", photo)

        live = inspect.getsource(PvLinearCapabilityRuntimeMixin.handle_event)
        self.assertLess(
            live.index("waiting_key == TWO_SCREENS_CHOICE_STEP_KEY"),
            live.index("handle_active_live_response"),
        )
        self.assertLess(
            live.index("waiting_key == LIVE_OPTIN_STEP_KEY"),
            live.index("handle_active_live_response"),
        )

    def test_rollback_switch_keeps_new_capabilities_inert_when_off(self):
        for method_name in (
            "action_send_linear_balloon",
            "handle_event",
            "action_auto_queue_two_screens_photo",
            "action_send_two_screens_photo",
            "action_send_live_optin",
        ):
            source = inspect.getsource(
                getattr(PvLinearCapabilityRuntimeMixin, method_name)
            )
            self.assertIn("linear_flow_enabled()", source, method_name)
            self.assertIn("super()", source, method_name)


if __name__ == "__main__":
    unittest.main()
