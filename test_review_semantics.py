import inspect
import unittest
from pathlib import Path

from telethon import errors

from gr_observer.outbox import (
    DEFINITIVE_RPC_ERRORS,
    AmbiguousExternalEffect,
    DefinitiveExternalEffectError,
    TelegramEffects,
    action_lane,
)


ROOT = Path(__file__).resolve().parent
STORAGE = (ROOT / "gr_observer" / "storage.py").read_text(encoding="utf-8")


class ReviewSemanticsTests(unittest.TestCase):
    def test_pv_peer_is_an_independent_logical_lane(self):
        self.assertEqual(
            action_lane({"module_id": "pv_reply", "payload": {"peer": 111}}),
            "pv:111",
        )
        self.assertEqual(
            action_lane({"module_id": "pv_reply", "payload": {"peer": 222}}),
            "pv:222",
        )
        self.assertNotEqual(
            action_lane({"module_id": "pv_reply", "payload": {"peer": 111}}),
            action_lane({"module_id": "pv_reply", "payload": {"peer": 222}}),
        )

    def test_global_queue_claim_does_not_wait_for_one_peer(self):
        self.assertIn("actions.available_at<=NOW()", STORAGE)
        self.assertIn("ORDER BY actions.available_at,actions.id", STORAGE)
        self.assertIn("SKIP LOCKED", STORAGE)

    def test_conclusive_telegram_4xx_is_not_review(self):
        self.assertIn(errors.BadRequestError, DEFINITIVE_RPC_ERRORS)
        self.assertIn(errors.UnauthorizedError, DEFINITIVE_RPC_ERRORS)
        self.assertIn(errors.ForbiddenError, DEFINITIVE_RPC_ERRORS)
        self.assertIn(errors.NotFoundError, DEFINITIVE_RPC_ERRORS)
        source = inspect.getsource(TelegramEffects.perform)
        definitive = source.index("except DEFINITIVE_RPC_ERRORS as exc:")
        ambiguous = source.index("except BaseException as exc:")
        self.assertLess(definitive, ambiguous)
        definitive_block = source[definitive:ambiguous]
        self.assertIn("finish_effect", definitive_block)
        self.assertNotIn("review_effect", definitive_block)

    def test_only_uncertain_external_result_goes_to_review(self):
        source = inspect.getsource(TelegramEffects.perform)
        ambiguous = source[source.index("except BaseException as exc:"):]
        self.assertIn("review_effect", ambiguous)
        self.assertIn("AmbiguousExternalEffect", ambiguous)

    def test_local_deterministic_media_validation_is_not_ambiguous(self):
        source = inspect.getsource(TelegramEffects.send_catalogued_media)
        self.assertIn("DefinitiveExternalEffectError", source)

    def test_writer_distinguishes_failed_from_review(self):
        from gr_observer.outbox import OutboxWriter

        source = inspect.getsource(OutboxWriter.run)
        failed = source.index("except DefinitiveExternalEffectError as exc:")
        review = source.index("except AmbiguousExternalEffect as exc:")
        self.assertLess(failed, review)
        self.assertIn("fail_action", source[failed:review])
        self.assertIn("review_action", source[review:])


if __name__ == "__main__":
    unittest.main()
