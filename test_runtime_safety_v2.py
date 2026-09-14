"""Regression tests for the v2 USER-session RPC pacer."""

import inspect
import unittest

from gr_observer.outbox import OutboxWriter
from gr_observer.runtime_safety import ProductionSafetyMixin
from gr_observer.runtime_safety_v2 import (
    PacedTelegramClient,
    ProductionSafetyV2Mixin,
    RpcPacer,
    SafeOutboxWriterV2,
)


class RuntimeSafetyV2Tests(unittest.TestCase):
    def test_v2_extends_existing_production_safety(self):
        self.assertTrue(issubclass(ProductionSafetyV2Mixin, ProductionSafetyMixin))
        self.assertTrue(issubclass(SafeOutboxWriterV2, OutboxWriter))

    def test_user_client_has_global_rpc_pacer(self):
        self.assertTrue(hasattr(PacedTelegramClient, "__call__"))
        source = inspect.getsource(PacedTelegramClient.__call__)
        self.assertIn("rpc_pacer.wait", source)
        self.assertIn("super().__call__", source)

    def test_pacer_is_serial_and_nonzero(self):
        pacer = RpcPacer()
        self.assertGreater(pacer.minimum_interval, 0)
        self.assertTrue(hasattr(pacer, "_lock"))

    def test_writer_does_not_globally_sleep_on_account_cooldown(self):
        source = inspect.getsource(SafeOutboxWriterV2.run)
        self.assertNotIn("account_remaining", source)
        self.assertIn("DeferredTraffic", source)
        self.assertIn('action["module_id"] != "core"', source)

    def test_runtime_still_creates_exactly_one_user_client_and_writer(self):
        source = inspect.getsource(ProductionSafetyV2Mixin.user_runtime)
        self.assertEqual(source.count("PacedTelegramClient("), 1)
        self.assertEqual(source.count("SafeOutboxWriterV2("), 1)
        self.assertIn("StringSession(self.settings.user_session_string)", source)


if __name__ == "__main__":
    unittest.main()
