"""Regression contract for the PV multimedia editor layer."""

import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.pv_message_steps import PvMessageStepStore

ROOT = Path(__file__).resolve().parent


class PvMultimediaEditorTests(unittest.IsolatedAsyncioTestCase):
    async def test_set_payload_preserves_step_identity(self):
        pool = NS(fetchval=AsyncMock(return_value=17))
        store = PvMessageStepStore(pool, NS())
        store._ready = True
        outcome = await store.set_payload(
            17,
            "legenda",
            media_source_peer=100,
            media_source_message_id=200,
            media_kind="video",
        )
        self.assertEqual(outcome, "updated")
        sql = pool.fetchval.await_args.args[0]
        self.assertIn("UPDATE pv_message_steps", sql)
        self.assertIn("WHERE id=$1", sql)
        self.assertNotIn("DELETE", sql.upper())
        set_clause = sql.upper().split(" SET ", 1)[1].split(" WHERE ", 1)[0]
        self.assertNotIn("STEP_KEY=", set_clause)
        self.assertNotIn("POSITION=", set_clause)
        self.assertNotIn("MEDIAN_DELAY_SECONDS=", set_clause)

    async def test_plain_text_edit_clears_old_media_reference(self):
        pool = NS(fetchval=AsyncMock(return_value=18))
        store = PvMessageStepStore(pool, NS())
        store._ready = True
        await store.set_content(18, "novo texto")
        sql = pool.fetchval.await_args.args[0]
        self.assertIn("media_source_peer=NULL", sql)
        self.assertIn("media_source_message_id=NULL", sql)

    def test_editor_accepts_only_expected_lightweight_media_types(self):
        source = (ROOT / "gr_observer" / "pv_editor_policy.py").read_text(encoding="utf-8")
        for kind in ('return "photo"', 'return "video"', 'return "gif"'):
            self.assertIn(kind, source)
        self.assertIn("set_payload(", source)
        self.assertIn("media_source_message_id", source)

    def test_editor_uses_generic_content_label_without_changing_callbacks(self):
        policy = (ROOT / "gr_observer" / "pv_editor_policy.py").read_text(encoding="utf-8")
        panel = (ROOT / "gr_observer" / "pv_message_panel.py").read_text(encoding="utf-8")
        self.assertIn('button.text = "📎 Conteúdo"', policy)
        self.assertIn('pvm:text:', panel)
        self.assertIn('pvmx:text:', panel)

    def test_editor_keeps_media_in_telegram_not_postgres_binary(self):
        steps = (ROOT / "gr_observer" / "pv_message_steps.py").read_text(encoding="utf-8")
        policy = (ROOT / "gr_observer" / "pv_editor_policy.py").read_text(encoding="utf-8")
        self.assertNotIn("BYTEA", steps)
        self.assertNotIn("download_media", policy)
        self.assertNotIn("upload_file", policy)

    def test_pure_media_new_balloon_is_allowed(self):
        steps = (ROOT / "gr_observer" / "pv_message_steps.py").read_text(encoding="utf-8")
        self.assertIn("if not value and not has_media", steps)
        self.assertIn("media_source_peer: int | None = None", steps)


if __name__ == "__main__":
    unittest.main()
