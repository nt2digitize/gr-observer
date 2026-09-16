"""Targeted regression tests for active-group membership refresh."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from gr_observer.modules.radar import RadarModule as LegacyRadarModule
from gr_observer.modules.radar_passive import PassiveRadarModule


class ActiveGroupMembershipTests(unittest.IsolatedAsyncioTestCase):
    def module(self, rows):
        pool = NS(
            fetchrow=AsyncMock(side_effect=rows),
            execute=AsyncMock(),
        )
        return PassiveRadarModule(pool, NS(), AsyncMock()), pool

    async def test_banned_group_leaves_active_membership(self):
        module, pool = self.module(
            [
                {"target_chat_id": -100123},
                {"target_chat_id": None, "last_error": "UserBannedInChannelError"},
            ]
        )
        with patch.object(
            LegacyRadarModule,
            "audit_link",
            new=AsyncMock(return_value="inaccessible"),
        ):
            status = await module.audit_link(9)

        self.assertEqual(status, "inaccessible")
        pool.execute.assert_awaited_once()
        sql, chat_id = pool.execute.await_args.args
        self.assertIn("membership_status='left'", sql)
        self.assertEqual(chat_id, -100123)

    async def test_private_group_access_loss_leaves_active_membership(self):
        module, pool = self.module(
            [
                {"target_chat_id": -100456},
                {"target_chat_id": None, "last_error": "ChannelPrivateError"},
            ]
        )
        with patch.object(
            LegacyRadarModule,
            "audit_link",
            new=AsyncMock(return_value="inaccessible"),
        ):
            await module.audit_link(10)

        pool.execute.assert_awaited_once()

    async def test_not_joined_group_leaves_active_membership(self):
        module, pool = self.module(
            [
                {"target_chat_id": -100789},
                {"target_chat_id": -100789, "last_error": None},
            ]
        )
        with patch.object(
            LegacyRadarModule,
            "audit_link",
            new=AsyncMock(return_value="not_joined"),
        ):
            await module.audit_link(11)

        pool.execute.assert_awaited_once()

    async def test_transient_inaccessible_error_does_not_hide_group(self):
        module, pool = self.module(
            [
                {"target_chat_id": -100999},
                {"target_chat_id": None, "last_error": "TimeoutError"},
            ]
        )
        with patch.object(
            LegacyRadarModule,
            "audit_link",
            new=AsyncMock(return_value="inaccessible"),
        ):
            await module.audit_link(12)

        pool.execute.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
