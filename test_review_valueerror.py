import inspect
import unittest

from gr_observer.outbox import DEFINITIVE_RPC_ERRORS, TelegramEffects


class ReviewValueErrorTests(unittest.TestCase):
    def test_value_error_is_definitive_not_review(self):
        self.assertIn(ValueError, DEFINITIVE_RPC_ERRORS)
        source = inspect.getsource(TelegramEffects.perform)
        definitive = source.index("except DEFINITIVE_RPC_ERRORS as exc:")
        ambiguous = source.index("except BaseException as exc:")
        self.assertLess(definitive, ambiguous)


if __name__ == "__main__":
    unittest.main()
