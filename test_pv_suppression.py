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

    def test_admin_surface_exposes_specific_user_stop(self):
        source = inspect.getsource(GroupControlPanel._handle_pv_kill_switch)
        self.assertIn("/parar_usuario", source)
        self.assertIn("resolve_pv_user", source)
        self.assertIn("suppress_pv_user", source)
        self.assertIn("usuarios_parados", source)
        self.assertIn("list_suppressed", source)

    def test_empty_stop_command_opens_recent_contact_picker(self):
        source = inspect.getsource(GroupControlPanel._handle_pv_kill_switch)
        picker = inspect.getsource(GroupControlPanel._show_pv_stop_picker)
        self.assertIn("_show_pv_stop_picker", source)
        self.assertIn("recent_pv_users", picker)
        self.assertIn("pvstop:", picker)
        self.assertIn("Mais recentes primeiro", picker)

    def test_picker_callback_uses_same_persistent_kill_switch(self):
        source = inspect.getsource(GroupControlPanel.on_callback)
        self.assertIn(r'pvstop:(\d+)', source)
        self.assertIn("suppress_pv_user", source)
        self.assertIn('reason="admin_picker"', source)
        self.assertIn("_show_pv_stop_picker", source)

    def test_recent_picker_excludes_already_suppressed_contacts(self):
        source = inspect.getsource(recent_pv_users)
        self.assertIn("NOT EXISTS", source)
        self.assertIn("pv_suppressed_users", source)
        self.assertIn("ORDER BY c.last_inbound_at DESC", source)

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