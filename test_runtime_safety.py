"""Regression tests for passive Radar and centralized traffic safety."""

import inspect
import unittest
from pathlib import Path

from gr_observer.application import Observer
from gr_observer.modules.radar_passive import PassiveRadarModule
from gr_observer.outbox import OutboxWriter, TelegramEffects
from gr_observer.traffic import (
    TRAFFIC_SCHEMA,
    DeferredTraffic,
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

    def test_traffic_schema_is_additive_and_persistent(self):
        self.assertIn("CREATE TABLE IF NOT EXISTS traffic_cooldowns", TRAFFIC_SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS traffic_events", TRAFFIC_SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS system_signals", TRAFFIC_SCHEMA)
        self.assertNotIn("DROP TABLE", TRAFFIC_SCHEMA.upper())
        self.assertNotIn("TRUNCATE", TRAFFIC_SCHEMA.upper())

    def test_safe_writer_preserves_single_writer_type(self):
        self.assertTrue(issubclass(SafeOutboxWriter, OutboxWriter))
        self.assertTrue(issubclass(SafeTelegramEffects, TelegramEffects))

    def test_radar_runtime_policy_is_passive(self):
        source = inspect.getsource(PassiveRadarModule.on_connect)
        self.assertIn("self.scan_task = None", source)
        self.assertNotIn("scan_loop", source)
        self.assertNotIn("create_task", source)

    def test_application_owns_safe_runtime_directly(self):
        source = inspect.getsource(Observer.user_runtime)
        self.assertIn("DurableTelegramClient", source)
        self.assertIn("SafeOutboxWriter", source)
        self.assertNotIn("super().user_runtime", source)
        entrypoint = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("asyncio.run(Observer().run())", entrypoint)
        self.assertNotIn("RuntimeObserver", entrypoint)

    def test_floodwait_is_deferred_not_slept_inside_effect(self):
        source = inspect.getsource(SafeTelegramEffects.perform)
        self.assertIn("record_flood_wait", source)
        self.assertIn("DeferredTraffic", source)
        self.assertNotIn("asyncio.sleep", source)

    def test_floodwait_journal_is_preserved_not_deleted(self):
        source = inspect.getsource(SafeTelegramEffects.perform)
        governor = inspect.getsource(SafeOutboxWriter)
        self.assertNotIn("DELETE FROM telegram_effects", source)
        self.assertNotIn("DELETE FROM telegram_effects", governor)


if __name__ == "__main__":
    unittest.main()
