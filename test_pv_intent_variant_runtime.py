import inspect
import os
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from gr_observer.modules.pv_reply_production import PvReplyProduction
from gr_observer.pv_intent_variant_runtime import PvIntentVariantRuntimeMixin
from gr_observer.pv_linear_flow import PvLinearRuntimeMixin


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

    def test_production_composes_variant_runtime_after_linear_owner(self):
        mro = PvReplyProduction.__mro__
        self.assertIn(PvLinearRuntimeMixin, mro)
        self.assertIn(PvIntentVariantRuntimeMixin, mro)
        self.assertLess(mro.index(PvLinearRuntimeMixin), mro.index(PvIntentVariantRuntimeMixin))


class _CanonicalSendBase:
    def __init__(self, *args, **kwargs):
        self.canonical_send = AsyncMock(return_value={"sent": True, "message_id": 77})

    async def _send_row(self, *, effects, peer, row, origin_key, variables):
        return await self.canonical_send(
            effects=effects,
            peer=peer,
            row=row,
            origin_key=origin_key,
            variables=variables,
        )


class _VariantProbe(PvIntentVariantRuntimeMixin, _CanonicalSendBase):
    pass


class VariantFlagOffTests(unittest.IsolatedAsyncioTestCase):
    async def test_flag_off_is_exact_passthrough_and_never_selects_variant(self):
        probe = _VariantProbe(NS(pool=NS()), NS())
        probe._intent_variant_store_ready = True
        probe.intent_variant_store.select_for_action = AsyncMock(
            side_effect=AssertionError("variant selector must stay inert with flag OFF")
        )
        row = {"id": 10, "content": "fala canonica"}
        variables = {"_membership_repertoires": ["pre_join"]}
        effects = object()

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PV_LINEAR_FLOW_ENABLED", None)
            result = await probe._send_row(
                effects=effects,
                peer=42,
                row=row,
                origin_key="pv_reply:linear:42:1:10",
                variables=variables,
            )

        self.assertEqual(result, {"sent": True, "message_id": 77})
        probe.intent_variant_store.select_for_action.assert_not_awaited()
        probe.canonical_send.assert_awaited_once_with(
            effects=effects,
            peer=42,
            row=row,
            origin_key="pv_reply:linear:42:1:10",
            variables=variables,
        )


if __name__ == "__main__":
    unittest.main()
