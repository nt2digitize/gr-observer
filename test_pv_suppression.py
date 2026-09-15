import inspect
import unittest

from gr_observer.group_integration import GroupControlPanel
from gr_observer.priority_storage import PriorityStorage
from gr_observer.pv_suppression import (
    PV_SUPPRESSION_SCHEMA,
    normalize_target,
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


if __name__ == "__main__":
    unittest.main()
