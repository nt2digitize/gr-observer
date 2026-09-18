import unittest
from types import SimpleNamespace

from gr_observer.human_timing import MIN_WRITING_DELAY_SECONDS
from gr_observer.modules.pv_reply_production import PvReplyProduction
from gr_observer.pv_editor_policy import PvEditorPolicyMixin
from gr_observer.pv_message_steps import POSITION_GAP


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
        pool = FakePool({"link.preview", "weekly.link2"})
        module = PvReplyProduction.__new__(PvReplyProduction)
        module.storage = SimpleNamespace(pool=pool)
        module.settings = SimpleNamespace(pv_reply_delay_seconds=60)

        restored = await module._restore_missing_required_destinations()

        self.assertEqual(restored, ("link.preview", "weekly.link2"))
        self.assertEqual(len(pool.calls), 5)
        for sql, _args in pool.calls:
            self.assertIn("ON CONFLICT(step_key) DO NOTHING", sql)
            self.assertNotIn("UPDATE pv_message_steps", sql)
            self.assertNotIn("DELETE FROM pv_message_steps", sql)

    async def test_preview_and_live_destination_use_canonical_positions(self):
        pool = FakePool(set())
        module = PvReplyProduction.__new__(PvReplyProduction)
        module.storage = SimpleNamespace(pool=pool)
        module.settings = SimpleNamespace(pv_reply_delay_seconds=60)
        await module._restore_missing_required_destinations()
        rows = {args[0]: args for _sql, args in pool.calls}
        self.assertEqual(rows["link.preview"][4], "{preview_link}")
        self.assertEqual(rows["followup.link"][4], "{preview_link}")
        self.assertNotIn("reminder.link", rows)
        self.assertEqual(rows["weekly.link1"][4], "{preview_link}")
        self.assertEqual(rows["weekly.link2"][4], "{preview_link}")
        self.assertEqual(rows["live.link"][2], POSITION_GAP * 2)
        self.assertEqual(rows["live.link"][3], "Destino")
        self.assertGreaterEqual(rows["live.link"][5], MIN_WRITING_DELAY_SECONDS)


class EditorResolvedDestinationTests(unittest.TestCase):
    def test_placeholder_shows_real_runtime_destination(self):
        panel = PvEditorPolicyMixin.__new__(PvEditorPolicyMixin)
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
