"""Safety contract for generic PV balloon storage preparation."""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class PvBalloonSchemaTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "gr_observer" / "pv_message_steps.py").read_text(
            encoding="utf-8"
        )

    def test_media_reference_columns_are_optional_and_additive(self):
        for column in (
            "ADD COLUMN IF NOT EXISTS media_source_peer BIGINT",
            "ADD COLUMN IF NOT EXISTS media_source_message_id BIGINT",
            "ADD COLUMN IF NOT EXISTS media_kind TEXT",
        ):
            with self.subTest(column=column):
                self.assertIn(column, self.source)

    def test_existing_text_link_contract_is_unchanged(self):
        self.assertIn("CHECK(kind IN ('text','link'))", self.source)
        self.assertIn("content TEXT NOT NULL", self.source)
        self.assertIn("infer_kind(row[\"content\"])", self.source)

    def test_layer_has_no_destructive_migration(self):
        ddl = self.source.split('DDL = """', 1)[1].split('"""', 1)[0].upper()
        self.assertNotIn("DROP TABLE", ddl)
        self.assertNotIn("DROP COLUMN", ddl)
        self.assertNotIn("ALTER COLUMN", ddl)
        self.assertNotIn("DELETE FROM", ddl)

    def test_layer_does_not_create_media_storage_or_runtime(self):
        self.assertNotIn("BYTEA", self.source)
        self.assertNotIn("TelegramClient(", self.source)
        self.assertNotIn("OutboxWriter(", self.source)
        self.assertNotIn("create_task(", self.source)


if __name__ == "__main__":
    unittest.main()
