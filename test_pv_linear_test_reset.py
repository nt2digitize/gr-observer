import os
import unittest
from unittest.mock import patch

from gr_observer.pv_linear_test_reset import (
    apply_linear_test_resets,
    configured_reset_request,
)


class FakeConn:
    def __init__(self, sessions=None):
        self.sessions = dict(sessions or {})
        self.applied = set()
        self.closed = False

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query, *args):
        if "CREATE TABLE IF NOT EXISTS pv_linear_test_resets" in query:
            return "OK"
        if "INSERT INTO pv_linear_test_resets" in query:
            self.applied.add((str(args[0]), int(args[1])))
            return "OK"
        raise AssertionError(f"execute inesperado: {query}")

    async def fetchval(self, query, *args):
        if "SELECT 1 FROM pv_linear_test_resets" in query:
            return 1 if (str(args[0]), int(args[1])) in self.applied else None
        if "UPDATE pv_linear_sessions" in query and "RETURNING generation" in query:
            user_id = int(args[0])
            row = self.sessions[user_id]
            row["generation"] = int(row["generation"]) + 1
            row["status"] = "waiting_reply"
            row["current_step_id"] = None
            row["current_position"] = 0
            row["last_inbound_message_id"] = None
            return int(row["generation"])
        raise AssertionError(f"fetchval inesperado: {query}")

    async def fetchrow(self, query, *args):
        if "FROM pv_linear_sessions" in query:
            row = self.sessions.get(int(args[0]))
            return dict(row) if row is not None else None
        raise AssertionError(f"fetchrow inesperado: {query}")

    async def close(self):
        self.closed = True


class PvLinearTestResetTests(unittest.IsolatedAsyncioTestCase):
    def test_request_requires_targets_inside_test_scope(self):
        valid = {
            "PV_LINEAR_TEST_RESET_TOKEN": "homolog-001",
            "PV_LINEAR_TEST_RESET_USER_IDS": "8795995074",
            "PV_LINEAR_TEST_USER_IDS": "8795995074",
        }
        self.assertEqual(
            configured_reset_request(valid),
            ("homolog-001", (8795995074,)),
        )

        invalid = dict(valid)
        invalid["PV_LINEAR_TEST_RESET_USER_IDS"] = "8795995074,123"
        self.assertIsNone(configured_reset_request(invalid))

    async def test_reset_is_idempotent_and_increments_generation_once(self):
        user_id = 8795995074
        conn = FakeConn(
            {
                user_id: {
                    "generation": 3,
                    "status": "completed",
                    "current_step_id": 99,
                    "current_position": 900,
                    "last_inbound_message_id": 321,
                }
            }
        )

        async def connect(_database_url):
            return conn

        env = {
            "DATABASE_URL": "postgresql://example/test",
            "PV_LINEAR_TEST_RESET_TOKEN": "homolog-001",
            "PV_LINEAR_TEST_RESET_USER_IDS": str(user_id),
            "PV_LINEAR_TEST_USER_IDS": str(user_id),
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(await apply_linear_test_resets(connect=connect), 1)
            self.assertEqual(await apply_linear_test_resets(connect=connect), 0)

        row = conn.sessions[user_id]
        self.assertEqual(row["generation"], 4)
        self.assertEqual(row["status"], "waiting_reply")
        self.assertIsNone(row["current_step_id"])
        self.assertEqual(row["current_position"], 0)
        self.assertIsNone(row["last_inbound_message_id"])
        self.assertTrue(conn.closed)

    async def test_missing_session_is_recorded_without_creating_one(self):
        user_id = 8795995074
        conn = FakeConn()

        async def connect(_database_url):
            return conn

        env = {
            "DATABASE_URL": "postgresql://example/test",
            "PV_LINEAR_TEST_RESET_TOKEN": "homolog-missing",
            "PV_LINEAR_TEST_RESET_USER_IDS": str(user_id),
            "PV_LINEAR_TEST_USER_IDS": str(user_id),
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertEqual(await apply_linear_test_resets(connect=connect), 0)
            self.assertEqual(await apply_linear_test_resets(connect=connect), 0)

        self.assertIn(("homolog-missing", user_id), conn.applied)
        self.assertNotIn(user_id, conn.sessions)


if __name__ == "__main__":
    unittest.main()
