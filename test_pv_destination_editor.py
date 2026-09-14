"""Focused tests for editable message + destination delivery."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from gr_observer.modules.pv_reply_contacts import (
    DESTINATION_PAIR_MIGRATION_VERSION,
    PvReplyWithContacts,
)
from gr_observer.pv_message_runtime import PvMessageRuntimeMixin


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *_args):
        return False


class DestinationMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_migration_adds_editable_message_before_destination_once(self):
        conn = NS(
            transaction=lambda: _AsyncContext(None),
            fetchval=AsyncMock(return_value=None),
            execute=AsyncMock(),
        )
        pool = NS(acquire=lambda: _AsyncContext(conn))
        module = object.__new__(PvReplyWithContacts)
        module.storage = NS(pool=pool)

        await module._ensure_destination_pair()

        self.assertEqual(conn.execute.await_count, 3)
        update_sql = conn.execute.await_args_list[0].args[0]
        insert_sql = conn.execute.await_args_list[1].args[0]
        migration_sql = conn.execute.await_args_list[2].args[0]
        self.assertIn("label='Destino'", update_sql)
        self.assertIn("destination.message", insert_sql)
        self.assertIn("Mensagem do destino", insert_sql)
        self.assertIn("Aqui está 👇", insert_sql)
        self.assertIn("pv_message_step_migrations", migration_sql)
        self.assertEqual(
            conn.execute.await_args_list[2].args[1],
            DESTINATION_PAIR_MIGRATION_VERSION,
        )

    async def test_migration_does_not_recreate_deleted_rows_after_applied(self):
        conn = NS(
            transaction=lambda: _AsyncContext(None),
            fetchval=AsyncMock(return_value=1),
            execute=AsyncMock(),
        )
        pool = NS(acquire=lambda: _AsyncContext(conn))
        module = object.__new__(PvReplyWithContacts)
        module.storage = NS(pool=pool)

        await module._ensure_destination_pair()

        conn.execute.assert_not_awaited()


class DestinationRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_destination_does_not_require_old_live_campaign_link(self):
        pool = NS(fetchval=AsyncMock(return_value=False))
        storage = NS(
            pool=pool,
            live_recipient_allowed=AsyncMock(return_value=True),
        )
        module = object.__new__(PvReplyWithContacts)
        module.storage = storage
        module._run_block = AsyncMock(return_value={"sent": True})
        action = {
            "action_key": "pv_reply:live-link:1:42",
            "payload": {"peer": 42, "campaign_id": 1},
        }

        result = await module.action_send_live_link(action, NS())

        self.assertTrue(result["sent"])
        module._run_block.assert_awaited_once()
        kwargs = module._run_block.await_args.kwargs
        self.assertEqual(kwargs["block_key"], "live_link")
        self.assertEqual(kwargs["variables"], {})

    async def test_placeholder_keeps_legacy_dynamic_destination_behavior(self):
        pool = NS(fetchval=AsyncMock(return_value=True))
        module = object.__new__(PvReplyWithContacts)
        module.storage = NS(pool=pool)
        action = {
            "action_key": "pv_reply:live-link:1:42",
            "payload": {"peer": 42, "campaign_id": 1},
        }
        expected = {"sent": True, "legacy": True}

        with patch.object(
            PvMessageRuntimeMixin,
            "action_send_live_link",
            new=AsyncMock(return_value=expected),
        ) as legacy:
            result = await module.action_send_live_link(action, NS())

        self.assertEqual(result, expected)
        legacy.assert_awaited_once()


class DestinationUiContractTests(unittest.TestCase):
    def test_pair_remains_inside_existing_pv_rib(self):
        source = __import__(
            "pathlib"
        ).Path("gr_observer/modules/pv_reply_contacts.py").read_text(encoding="utf-8")
        self.assertIn("class PvReplyWithContacts(PvMessageRuntimeMixin, PvReplyModule)", source)
        self.assertNotIn("TelegramClient(", source)
        self.assertNotIn("OutboxWriter(", source)
        self.assertNotIn("create_task(", source)
        self.assertIn("Mensagem + destino", source)


if __name__ == "__main__":
    unittest.main()
