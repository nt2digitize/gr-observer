import tempfile
import unittest
from pathlib import Path

import generate_session as login


class SessionRotationTests(unittest.TestCase):
    def test_existing_file_is_archived_without_being_read_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "radar-gr-session.txt"
            destination.write_text("old-secret", encoding="utf-8")
            archived = login.archive_existing_session(destination)

            self.assertFalse(destination.exists())
            self.assertIsNotNone(archived)
            self.assertEqual(archived.read_text(encoding="utf-8"), "old-secret")
            self.assertTrue(archived.name.startswith("radar-gr-session-anterior-"))
            self.assertEqual(archived.suffix, ".txt")

    def test_no_archive_is_created_when_destination_does_not_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "radar-gr-session.txt"
            self.assertIsNone(login.archive_existing_session(destination))
            self.assertFalse(destination.exists())


if __name__ == "__main__":
    unittest.main()
