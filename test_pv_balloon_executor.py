"""Regression tests for the lightweight generic PV balloon executor."""

import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.pv_balloon_sender import has_media, media_reference, send_media_balloon

ROOT = Path(__file__).resolve().parent


class PvBalloonExecutorTests(unittest.IsolatedAsyncioTestCase):
    def test_media_reference_requires_both_telegram_ids(self):
        self.assertFalse(has_media({"media_source_peer": None, "media_source_message_id": None}))
        self.assertFalse(has_media({"media_source_peer": 1, "media_source_message_id": None}))
        self.assertEqual(
            media_reference({"media_source_peer": -1001, "media_source_message_id": 77}),
            (-1001, 77),
        )

    async def test_media_balloon_uses_existing_safe_effects(self):
        effects = NS(
            send_catalogued_media=AsyncMock(return_value={"message_id": 88}),
            send_text=AsyncMock(return_value={"message_id": 89}),
            send_text_preview=AsyncMock(return_value={"message_id": 90}),
        )
        row = {"media_source_peer": -1001, "media_source_message_id": 77}
        result = await send_media_balloon(
            effects=effects,
            peer=42,
            row=row,
            text="legenda",
            effect_key="pv:test:message:1",
        )
        effects.send_catalogued_media.assert_awaited_once_with(
            -1001, 77, 42, "pv:test:message:1:media", spoiler=False, ttl_seconds=None
        )
        effects.send_text.assert_awaited_once_with(42, "legenda", "pv:test:message:1:text")
        self.assertEqual(result["media_message_id"], 88)
        self.assertEqual(result["text_message_id"], 89)

    async def test_link_companion_preserves_preview_policy(self):
        effects = NS(
            send_catalogued_media=AsyncMock(return_value={"message_id": 10}),
            send_text=AsyncMock(),
            send_text_preview=AsyncMock(return_value={"message_id": 11}),
        )
        await send_media_balloon(
            effects=effects,
            peer=42,
            row={"media_source_peer": 5, "media_source_message_id": 6},
            text="https://example.com/x",
            effect_key="k",
        )
        effects.send_text_preview.assert_awaited_once()
        effects.send_text.assert_not_awaited()

    def test_production_text_rows_keep_exact_legacy_path(self):
        source = (ROOT / "gr_observer" / "modules" / "pv_reply_production.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("if not has_media(row):", source)
        self.assertIn("return await super()._send_row(", source)

    def test_production_media_rows_keep_existing_return_contract(self):
        source = (ROOT / "gr_observer" / "modules" / "pv_reply_production.py").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            'return {"sent": True, "step_id": int(row["id"]), **result}',
            source,
        )
        self.assertNotIn('"membership_state":', source)
        self.assertNotIn('"membership_repertoires":', source)

    def test_helper_does_not_mutate_telegram_directly(self):
        source = (ROOT / "gr_observer" / "pv_balloon_sender.py").read_text(encoding="utf-8")
        self.assertNotIn("TelegramClient", source)
        self.assertNotIn("send_message(", source)
        self.assertNotIn("send_file(", source)
        self.assertNotIn("forward_messages(", source)
        self.assertNotIn("OutboxWriter", source)


if __name__ == "__main__":
    unittest.main()
