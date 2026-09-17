import inspect
import unittest
from decimal import Decimal
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.pv_linear_flow import (
    PREVIEW_LINK_STEP_KEY,
    PvLinearConversationStore,
    PvLinearRuntimeMixin,
)
from gr_observer.pv_linear_panel import PvLinearPanelMixin


class PvLinearPreviewGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_before_preview_is_rejected(self):
        pool = NS(
            fetchval=AsyncMock(
                side_effect=[Decimal("1000000"), Decimal("2000000")]
            )
        )
        store = PvLinearConversationStore(pool)
        store._ready = True

        changed = await store.set_wait_for_reply(10, True)

        self.assertFalse(changed)
        self.assertEqual(pool.fetchval.await_count, 2)

    async def test_wait_at_or_after_preview_is_allowed(self):
        pool = NS(
            fetchval=AsyncMock(
                side_effect=[Decimal("2000000"), Decimal("2000000"), 10]
            )
        )
        store = PvLinearConversationStore(pool)
        store._ready = True

        changed = await store.set_wait_for_reply(10, True)

        self.assertTrue(changed)
        self.assertEqual(pool.fetchval.await_count, 3)

    async def test_disabling_wait_never_depends_on_preview_position(self):
        pool = NS(fetchval=AsyncMock(return_value=10))
        store = PvLinearConversationStore(pool)
        store._ready = True

        changed = await store.set_wait_for_reply(10, False)

        self.assertTrue(changed)
        self.assertEqual(pool.fetchval.await_count, 1)

    async def test_reorder_normalizer_targets_only_pre_preview_waits(self):
        conn = NS(execute=AsyncMock())

        await PvLinearConversationStore._clear_pre_preview_waits(conn)

        sql, key = conn.execute.await_args.args
        self.assertEqual(key, PREVIEW_LINK_STEP_KEY)
        self.assertIn("wait_for_reply=FALSE", sql)
        self.assertIn("linear_position <", sql)


class PvLinearPreviewGateContractTests(unittest.TestCase):
    def test_runtime_rechecks_wait_policy_before_stopping_lane(self):
        source = inspect.getsource(PvLinearRuntimeMixin.action_send_linear_balloon)
        self.assertIn("wait_for_reply_allowed", source)
        self.assertIn("status='waiting_reply'", source)

    def test_reorder_reapplies_preview_gate_policy(self):
        source = inspect.getsource(PvLinearConversationStore.move)
        self.assertIn("_clear_pre_preview_waits", source)

    def test_editor_explains_why_pre_preview_wait_is_refused(self):
        source = inspect.getsource(PvLinearPanelMixin.on_callback)
        self.assertIn(
            "Antes do link da prévia, a conversa continua automaticamente.",
            source,
        )


if __name__ == "__main__":
    unittest.main()
