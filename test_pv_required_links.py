import unittest
from types import SimpleNamespace

from gr_observer.pv_editor_safety import PvEditorSafetyMixin
from gr_observer.pv_production_guard import (
    repair_required_destination_steps,
    required_destination_steps,
)


class FakePool:
    def __init__(self, missing):
        self.missing = set(missing)
        self.calls = []

    async def fetchval(self, sql, *args):
        self.calls.append((sql, args))
        step_key = str(args[0])
        return step_key if step_key in self.missing else None


class RequiredDestinationRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_repairs_only_missing_rows_with_conflict_safe_insert(self):
        settings = SimpleNamespace(pv_reply_delay_seconds=60)
        pool = FakePool({"link.preview", "weekly.link2"})

        restored = await repair_required_destination_steps(pool, settings)

        self.assertEqual(restored, ("link.preview", "weekly.link2"))
        self.assertEqual(len(pool.calls), len(required_destination_steps(settings)))
        for sql, _args in pool.calls:
            self.assertIn("ON CONFLICT(step_key) DO NOTHING", sql)
            self.assertNotIn("UPDATE pv_message_steps", sql)
            self.assertNotIn("DELETE FROM pv_message_steps", sql)

    async def test_required_preview_path_is_present(self):
        settings = SimpleNamespace(pv_reply_delay_seconds=60)
        rows = {item[0]: item for item in required_destination_steps(settings)}
        self.assertEqual(rows["link.preview"][5], "{preview_link}")
        self.assertEqual(rows["followup.link"][5], "{preview_link}")
        self.assertEqual(rows["reminder.link"][5], "{preview_link}")
        self.assertEqual(rows["weekly.link1"][5], "{preview_link}")
        self.assertEqual(rows["weekly.link2"][5], "{preview_link}")


class EditorResolvedDestinationTests(unittest.TestCase):
    def test_placeholder_shows_real_runtime_destination(self):
        panel = PvEditorSafetyMixin.__new__(PvEditorSafetyMixin)
        panel.app = SimpleNamespace(
            settings=SimpleNamespace(pv_preview_link="https://t.me/example_preview")
        )
        self.assertEqual(
            panel._resolved_destination("{preview_link}"),
            "https://t.me/example_preview",
        )
        self.assertIsNone(panel._resolved_destination("fala sem marcador"))


if __name__ == "__main__":
    unittest.main()
