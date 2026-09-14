"""Regression tests for passive Radar and centralized traffic safety."""

import inspect
import unittest
from pathlib import Path

from gr_observer.outbox import OutboxWriter, TelegramEffects
from gr_observer.runtime_safety import (
    SAFETY_SCHEMA,
    DeferredTraffic,
    ProductionSafetyMixin,
    SafeOutboxWriter,
    SafeTelegramEffects,
    telegram_links,
)


ROOT = Path(__file__).resolve().parent


class RuntimeSafetyTests(unittest.TestCase):
    def test_defer_control_flow_bypasses_feature_exception_handlers(self):
        self.assertTrue(issubclass(DeferredTraffic, BaseException))
        self.assertFalse(issubclass(DeferredTraffic, Exception))

    def test_telegram_links_are_extracted_without_network_resolution(self):
        self.assertEqual(
            telegram_links("olha t.me/+AbC_123 e https://t.me/grupo_teste."),
            ("t.me/+AbC_123", "https://t.me/grupo_teste"),
        )

    def test_safety_schema_is_additive_and_persistent(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS traffic_cooldowns", SAFETY_SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS traffic_events", SAFETY_SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS system_signals", SAFETY_SCHEMA)
        self.assertNotIn("DROP TABLE", SAFETY_SCHEMA.upper())
        self.assertNotIn("TRUNCATE", SAFETY_SCHEMA.upper())

    def test_safe_writer_preserves_single_writer_type(self):
        self.assertTrue(issubclass(SafeOutboxWriter, OutboxWriter))
        self.assertTrue(issubclass(SafeTelegramEffects, TelegramEffects))

    def test_radar_runtime_policy_is_passive(self):
        source = inspect.getsource(ProductionSafetyMixin._connect_module)
        self.assertIn('module_id != "radar"', source)
        self.assertIn("radar.scan_task = None", source)
        self.assertNotIn("scan_loop()", source)

    def test_railway_entrypoint_installs_safety_mixin(self):
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("ProductionSafetyMixin", source)
        self.assertIn("DurablePeerObserverMixin", source)
        self.assertIn("FloodAwareObserverMixin", source)

    def test_floodwait_is_deferred_not_slept_inside_effect(self):
        source = inspect.getsource(SafeTelegramEffects.perform)
        self.assertIn("record_flood_wait", source)
        self.assertIn("raise DeferredTraffic", source)
        self.assertNotIn("asyncio.sleep", source)


if __name__ == "__main__":
    unittest.main()
