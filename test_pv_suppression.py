import inspect
import unittest

from gr_observer.group_integration import GroupControlPanel
from gr_observer.priority_storage import PriorityStorage
from gr_observer.pv_suppression import (
    PV_SUPPRESSION_SCHEMA,
    normalize_target,
    recent_pv_users,
    suppress_pv_user,
)


class PvSuppressionPolicyTests(unittest.TestCase):
    def test_target_accepts_numeric_id_or_username(self):
        self.assertEqual(normalize_target("  123456  "), "123456")
        self.assertEqual(normalize_target(" @GoldenRose "), "GoldenRose")

    def test_schema_is_additive(self):
        upper = PV_SUPPRESSION_SCHEMA.upper()
        self.assertIn("CREATE TABLE IF NOT EXISTS PV_SUPPRESSED_USERS", upper)
        self.assertNotIn("DROP ", upper)
        self.assertNotIn("TRUNCATE ", upper)
        self.assertNotIn("DELETE ", upper)

    def test_stop_preserves_outbox_history(self):
        source = inspect.getsource(suppress_pv_user)
        self.assertIn("stage='stopped'", source)
        self.assertIn("status='unsubscribed'", source)
        self.assertIn("status='succeeded'", source)
        self.assertIn("admin_suppressed", source)
        self.assertNotIn("DELETE FROM outbox_actions", source)

    def test_ingress_is_blocked_before_pv_state_machine(self):
        source = inspect.getsource(PriorityStorage.accept_pv_message)
        self.assertIn("is_suppressed", source)
        self.assertIn('return "suppressed"', source)
        self.assertIn("super().accept_pv_message", source)

    def test_claim_neutralizes_and_excludes_suppressed_peer(self):
        source = inspect.getsource(PriorityStorage.claim_next_action)
        self.assertIn("pv_suppressed_users", source)
        self.assertIn("admin_suppressed", source)
        self.assertIn("actions.module_id='pv_reply'", source)
        self.assertIn("FOR UPDATE OF actions SKIP LOCKED", source)

    def test_admin_surface_exposes_specific_user_stop_with_confirmation(self):
        command = inspect.getsource(GroupControlPanel._handle_pv_kill_switch)
        callback = inspect.getsource(GroupControlPanel.on_callback)
        self.assertIn("/parar_usuario", command)
        self.assertIn("resolve_pv_user", command)
        self.assertIn("_show_pv_stop_confirmation", command)
        self.assertIn("usuarios_parados", command)
        self.assertIn("list_suppressed", command)
        self.assertIn("pvstop:confirm", callback)
        self.assertIn("suppress_pv_user", callback)

    def test_empty_stop_command_opens_picker_with_preventive_options(self):
        source = inspect.getsource(GroupControlPanel._handle_pv_kill_switch)
        picker = inspect.getsource(GroupControlPanel._show_pv_stop_picker)
        self.assertIn("_show_pv_stop_picker", source)
        self.assertIn("recent_pv_users", picker)
        self.assertIn("pvstop:pick:", picker)
        self.assertIn("pvstop:search", picker)
        self.assertIn("pvstop:forward", picker)
        self.assertIn("antes de ele entrar no funil", picker)

    def test_picker_and_forward_require_confirmation_before_persistent_stop(self):
        callback = inspect.getsource(GroupControlPanel.on_callback)
        pending = inspect.getsource(GroupControlPanel._handle_pv_stop_pending_input)
        self.assertIn(r'pvstop:pick:(\d+)', callback)
        self.assertIn(r'pvstop:confirm:(\d+)', callback)
        self.assertIn("_show_pv_stop_confirmation", callback)
        self.assertIn("suppress_pv_user", callback)
        self.assertIn("_forwarded_user_id", pending)
        self.assertIn("ninguém foi pausado", pending)

    def test_recent_picker_includes_known_contacts_and_excludes_suppressed(self):
        source = inspect.getsource(recent_pv_users)
        self.assertIn("contact_ledger", source)
        self.assertIn("pv_reply_contacts", source)
        self.assertIn("NOT EXISTS", source)
        self.assertIn("pv_suppressed_users", source)
        self.assertIn("ORDER BY c.seen_at DESC", source)

    def test_picker_label_disambiguates_with_id_suffix(self):
        label = GroupControlPanel._pv_stop_label(
            {
                "user_id": 123456789,
                "username": "joao",
                "display_name": "João",
            }
        )
        self.assertIn("João", label)
        self.assertIn("@joao", label)
        self.assertTrue(label.endswith("6789"))
        self.assertLessEqual(len(label), 60)


if __name__ == "__main__":
    unittest.main()
