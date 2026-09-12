import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import generate_session as login


class SessionRotationTests(unittest.TestCase):
    def test_existing_file_is_archived_without_being_read_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "radar-gr-session.txt"
            destination.write_text("old-secret", encoding="utf-8")
            with patch.object(login.datetime, "now") as now:
                now.return_value.strftime.return_value = "20260911-220000"
                archived = login.archive_existing_session(destination)

            self.assertFalse(destination.exists())
            self.assertIsNotNone(archived)
            self.assertEqual(archived.read_text(encoding="utf-8"), "old-secret")
            self.assertIn("radar-gr-session-anterior-20260911-220000", archived.name)

    def test_no_archive_is_created_when_destination_does_not_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "radar-gr-session.txt"
            self.assertIsNone(login.archive_existing_session(destination))
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
