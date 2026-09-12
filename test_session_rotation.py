import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


class AtomicRotationTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_session_stays_active_until_new_file_is_generated(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "radar-gr-session.txt"
            destination.write_text("old-secret", encoding="utf-8")

            async def fake_generate(_api_id, _api_hash, _phone, pending):
                self.assertEqual(destination.read_text(encoding="utf-8"), "old-secret")
                pending.write_text("new-secret", encoding="utf-8")

            with patch.object(login, "generate_session", new=AsyncMock(side_effect=fake_generate)):
                archived = await login.generate_rotated_session(
                    123, "a" * 32, "+12025550123", destination
                )

            self.assertEqual(destination.read_text(encoding="utf-8"), "new-secret")
            self.assertIsNotNone(archived)
            self.assertEqual(archived.read_text(encoding="utf-8"), "old-secret")
            self.assertFalse((Path(directory) / ".radar-gr-session.txt.new").exists())

    async def test_generation_failure_does_not_move_old_session(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "radar-gr-session.txt"
            destination.write_text("old-secret", encoding="utf-8")
            with patch.object(
                login,
                "generate_session",
                new=AsyncMock(side_effect=login.SetupError("falhou")),
            ):
                with self.assertRaises(login.SetupError):
                    await login.generate_rotated_session(
                        123, "a" * 32, "+12025550123", destination
                    )
            self.assertEqual(destination.read_text(encoding="utf-8"), "old-secret")


if __name__ == "__main__":
    unittest.main()
