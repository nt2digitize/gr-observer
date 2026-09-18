import inspect
import unittest

from gr_observer.pv_intent_variant_runtime import PvIntentVariantRuntimeMixin


class VariantRuntimeSafetyTests(unittest.TestCase):
    def test_runtime_adapter_does_not_own_progression_or_telegram(self):
        source = inspect.getsource(PvIntentVariantRuntimeMixin).casefold()
        for token in (
            "outbox_actions",
            "next_step",
            "pv_linear_sessions",
            "telegramclient",
            "send_message(",
            "send_file(",
            "create_task(",
        ):
            self.assertNotIn(token, source)

    def test_selection_is_linear_only_and_retry_key_is_existing_origin_key(self):
        source = inspect.getsource(PvIntentVariantRuntimeMixin._send_row)
        self.assertIn("linear_flow_enabled()", source)
        self.assertIn('startswith("pv_reply:linear:")', source)
        self.assertIn("action_key=str(origin_key)", source)
        self.assertIn("_membership_repertoires", source)

    def test_variant_failure_falls_back_to_canonical_super_send(self):
        source = inspect.getsource(PvIntentVariantRuntimeMixin._send_row)
        self.assertIn("except Exception", source)
        self.assertIn("return await super()._send_row(", source)
        self.assertIn("effective_row = row", source)

    def test_runtime_creates_no_second_client_writer_or_worker(self):
        source = inspect.getsource(PvIntentVariantRuntimeMixin)
        for token in ("TelegramClient(", "SafeOutboxWriter(", "OutboxWriter(", "create_task("):
            self.assertNotIn(token, source)


if __name__ == "__main__":
    unittest.main()
