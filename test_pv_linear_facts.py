import inspect
import unittest
from datetime import datetime, timezone

from gr_observer.pv_linear_facts import (
    PreviewLinkDelivery,
    latest_preview_link_delivery,
)


class FakePool:
    def __init__(self, row=None):
        self.row = row
        self.query = None
        self.args = None

    async def fetchrow(self, query, *args):
        self.query = query
        self.args = args
        return self.row


class PvLinearFactsTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_delivery_means_no_fact(self):
        pool = FakePool(None)
        self.assertIsNone(await latest_preview_link_delivery(pool, 42))
        self.assertEqual(pool.args, ("42",))

    async def test_delivery_comes_from_successful_effect_journal(self):
        sent_at = datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)
        pool = FakePool(
            {
                "sent_at": sent_at,
                "message_id": 123,
                "destination_url": "https://t.me/+abc",
                "target_chat_id": -100987,
            }
        )
        fact = await latest_preview_link_delivery(pool, 42)
        self.assertEqual(
            fact,
            PreviewLinkDelivery(
                sent_at=sent_at,
                message_id=123,
                destination_url="https://t.me/+abc",
                target_chat_id=-100987,
            ),
        )
        sql = pool.query
        self.assertIn("telegram_effects", sql)
        self.assertIn("e.status='succeeded'", sql)
        self.assertIn("send_linear_balloon", sql)
        self.assertIn("link.preview", sql)
        self.assertIn("link_targets", sql)

    async def test_unknown_link_mapping_keeps_success_timestamp(self):
        sent_at = datetime(2026, 9, 17, 20, 0, tzinfo=timezone.utc)
        pool = FakePool(
            {
                "sent_at": sent_at,
                "message_id": None,
                "destination_url": "https://t.me/+unknown",
                "target_chat_id": None,
            }
        )
        fact = await latest_preview_link_delivery(pool, 7)
        self.assertEqual(fact.sent_at, sent_at)
        self.assertIsNone(fact.target_chat_id)


class PvLinearFactsSafetyTests(unittest.TestCase):
    def test_read_model_has_no_schema_or_mutation(self):
        source = inspect.getsource(latest_preview_link_delivery).upper()
        for token in ("INSERT ", "UPDATE ", "DELETE ", "DROP ", "ALTER ", "CREATE TABLE"):
            self.assertNotIn(token, source)
        self.assertNotIn("OUTBOX_ACTIONS(", source)


if __name__ == "__main__":
    unittest.main()
