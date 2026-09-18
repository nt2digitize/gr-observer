import os
import unittest
from unittest.mock import patch

from gr_observer.pv_linear_flow import linear_flow_enabled


class FakeEvent:
    def __init__(self, sender_id: int):
        self.sender_id = sender_id


def enabled_with_peer_local(value: int) -> bool:
    peer = value
    return linear_flow_enabled()


def enabled_with_action(value: int) -> bool:
    action = {"payload": {"peer": value}}
    return linear_flow_enabled()


def enabled_with_event(value: int) -> bool:
    event = FakeEvent(value)
    return linear_flow_enabled()


def enabled_with_kwargs_action(value: int) -> bool:
    kwargs = {"action": {"payload": {"peer": value}}}
    return linear_flow_enabled()


class LinearControlledScopeTests(unittest.TestCase):
    def test_flag_off_disables_everything(self):
        with patch.dict(
            os.environ,
            {"PV_LINEAR_FLOW_ENABLED": "false", "PV_LINEAR_TEST_USER_IDS": "123"},
            clear=True,
        ):
            self.assertFalse(linear_flow_enabled(123))
            self.assertFalse(enabled_with_event(123))

    def test_no_scope_preserves_full_rollout_semantics(self):
        with patch.dict(os.environ, {"PV_LINEAR_FLOW_ENABLED": "true"}, clear=True):
            self.assertTrue(linear_flow_enabled())
            self.assertTrue(linear_flow_enabled(123))
            self.assertTrue(enabled_with_event(999))

    def test_scope_allows_only_explicit_target(self):
        with patch.dict(
            os.environ,
            {"PV_LINEAR_FLOW_ENABLED": "true", "PV_LINEAR_TEST_USER_IDS": "123"},
            clear=True,
        ):
            self.assertTrue(linear_flow_enabled(123))
            self.assertFalse(linear_flow_enabled(456))
            self.assertFalse(linear_flow_enabled())

    def test_scope_infers_existing_runtime_call_shapes(self):
        with patch.dict(
            os.environ,
            {"PV_LINEAR_FLOW_ENABLED": "true", "PV_LINEAR_TEST_USER_IDS": "123"},
            clear=True,
        ):
            self.assertTrue(enabled_with_peer_local(123))
            self.assertTrue(enabled_with_action(123))
            self.assertTrue(enabled_with_event(123))
            self.assertTrue(enabled_with_kwargs_action(123))
            self.assertFalse(enabled_with_peer_local(456))
            self.assertFalse(enabled_with_action(456))
            self.assertFalse(enabled_with_event(456))
            self.assertFalse(enabled_with_kwargs_action(456))

    def test_scope_accepts_multiple_ids_and_separators(self):
        with patch.dict(
            os.environ,
            {
                "PV_LINEAR_FLOW_ENABLED": "yes",
                "PV_LINEAR_TEST_USER_IDS": "123, 456;789",
            },
            clear=True,
        ):
            self.assertTrue(linear_flow_enabled(123))
            self.assertTrue(linear_flow_enabled(456))
            self.assertTrue(linear_flow_enabled(789))
            self.assertFalse(linear_flow_enabled(999))

    def test_nonblank_invalid_scope_fails_closed(self):
        with patch.dict(
            os.environ,
            {
                "PV_LINEAR_FLOW_ENABLED": "true",
                "PV_LINEAR_TEST_USER_IDS": "abc,-1,0",
            },
            clear=True,
        ):
            self.assertFalse(linear_flow_enabled())
            self.assertFalse(linear_flow_enabled(123))
            self.assertFalse(enabled_with_event(123))


if __name__ == "__main__":
    unittest.main()
