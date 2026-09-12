import io
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from PIL import Image
from telethon import functions, types

from gr_observer.outbox import TelegramEffects, prepare_courtesy_photo
from gr_observer.pv_journey import PvJourneyStore
from gr_observer.pv_photo_flow import PvPhotoFlowStore


class AsyncTransaction:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class AsyncAcquire:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConn:
    def __init__(self, *, fetchvals=None, fetchrows=None, fetches=None):
        self.fetchvals = list(fetchvals or [])
        self.fetchrows = list(fetchrows or [])
        self.fetches = list(fetches or [])
        self.executed = []

    def transaction(self):
        return AsyncTransaction()

    async def fetchval(self, sql, *args):
        if not self.fetchvals:
            raise AssertionError(f"fetchval inesperado: {sql}")
        return self.fetchvals.pop(0)

    async def fetchrow(self, sql, *args):
        if not self.fetchrows:
            raise AssertionError(f"fetchrow inesperado: {sql}")
        return self.fetchrows.pop(0)

    async def fetch(self, sql, *args):
        if not self.fetches:
            raise AssertionError(f"fetch inesperado: {sql}")
        return self.fetches.pop(0)

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        return AsyncAcquire(self.conn)


class AutonomousJourneyTests(unittest.IsolatedAsyncioTestCase):
    async def test_greeting_atomically_queues_link_without_human_reply(self):
        conn = FakeConn(fetchvals=[42])
        store = PvJourneyStore(FakePool(conn))

        advanced = await store.greeting_sent_and_schedule_link(
            user_id=42,
            delay_seconds=60,
        )

        self.assertTrue(advanced)
        self.assertEqual(len(conn.executed), 1)
        sql, args = conn.executed[0]
        self.assertIn("'send_link'", sql)
        self.assertEqual(args[0], "pv_reply:auto-link:42")
        self.assertIn('"peer":42', args[1])
        self.assertEqual(args[2], 60)

    async def test_choice_window_arms_durable_no_reply_fallback(self):
        conn = FakeConn(fetchvals=[42])
        store = PvPhotoFlowStore(FakePool(conn))

        advanced = await store.open_choice_window_and_schedule_auto_photo(
            user_id=42,
            expected_status="prompt_queued",
            action_key="pv_reply:two-screens:auto-photo:42",
            delay_seconds=6,
        )

        self.assertTrue(advanced)
        self.assertEqual(len(conn.executed), 1)
        sql, args = conn.executed[0]
        self.assertIn("auto_queue_two_screens_photo", sql)
        self.assertEqual(args[0], "pv_reply:two-screens:auto-photo:42")
        self.assertEqual(args[2], 6)

    async def test_no_reply_fallback_queues_one_available_unsent_photo(self):
        conn = FakeConn(
            fetchrows=[{"status": "awaiting_choice"}],
            fetches=[
                [{"slot": "peitos"}, {"slot": "cu"}],
                [],
            ],
        )
        store = PvPhotoFlowStore(FakePool(conn))

        outcome = await store.auto_queue_first_photo(42)

        self.assertEqual(outcome, "photo_queued")
        self.assertEqual(len(conn.executed), 2)
        update_sql, update_args = conn.executed[0]
        self.assertIn("status='photo_queued'", update_sql)
        self.assertIn(update_args[1], {"peitos", "cu"})
        queue_sql, queue_args = conn.executed[1]
        self.assertIn("send_two_screens_photo", queue_sql)
        self.assertEqual(queue_args[0], "pv_reply:two-screens:photo:auto:42:1")
        self.assertIn('"peer":42', queue_args[1])


class PreviewClient:
    def __init__(self):
        self.requests = []

    async def get_input_entity(self, peer):
        return types.InputPeerUser(user_id=int(peer), access_hash=1)

    async def __call__(self, request):
        self.requests.append(request)
        return NS(id=99)


class LinkPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_effect_allows_telegram_webpage_card(self):
        storage = NS(
            begin_effect=AsyncMock(return_value=("execute", None)),
            finish_effect=AsyncMock(),
            review_effect=AsyncMock(),
        )
        client = PreviewClient()
        effects = TelegramEffects(storage, client, 1)

        await effects.send_text_preview(42, "https://t.me/example", "preview:42")

        request = client.requests[-1]
        self.assertIsInstance(request, functions.messages.SendMessageRequest)
        self.assertFalse(request.no_webpage)


class CourtesyPhotoTests(unittest.TestCase):
    def test_large_source_becomes_1080px_jpeg_copy(self):
        source = Image.new("RGB", (1600, 1200), "white")
        raw = io.BytesIO()
        source.save(raw, format="PNG")

        prepared = prepare_courtesy_photo(raw.getvalue())

        with Image.open(io.BytesIO(prepared)) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertLessEqual(max(result.size), 1080)
        self.assertLess(len(prepared), len(raw.getvalue()))


if __name__ == "__main__":
    unittest.main()
