"""Production Radar policy: event-driven observation with no periodic crawler."""

from __future__ import annotations

import logging

from .radar import RadarModule as LegacyRadarModule

log = logging.getLogger("gr-observer.radar-passive")


class PassiveRadarModule(LegacyRadarModule):
    """Use Radar's event handlers and targeted audits without auto-scanning dialogs."""

    connection_reason = "Ligado — passivo, sem varredura periódica"
    _MEMBERSHIP_LOST_ERRORS = {
        "UserBannedInChannelError",
        "ChannelPrivateError",
    }

    async def on_connect(self, client, me) -> None:
        self.client = client
        self.me = me
        self.scan_task = None
        log.info("Radar conectado em modo passivo; crawler periódico não iniciado")

    async def on_disconnect(self) -> None:
        self.client = None
        self.me = None
        self.scan_task = None

    async def audit_link(self, link_id: int) -> str:
        """Keep compact active-group lists aligned with a targeted link audit."""
        before = await self.pool.fetchrow(
            "SELECT target_chat_id FROM link_targets WHERE id=$1", int(link_id)
        )
        previous_chat_id = (
            int(before["target_chat_id"])
            if before and before["target_chat_id"] is not None
            else None
        )

        status = await super().audit_link(link_id)

        after = await self.pool.fetchrow(
            "SELECT target_chat_id,last_error FROM link_targets WHERE id=$1",
            int(link_id),
        )
        current_chat_id = (
            int(after["target_chat_id"])
            if after and after["target_chat_id"] is not None
            else None
        )
        chat_id = current_chat_id or previous_chat_id
        error = str(after["last_error"] or "") if after else ""
        membership_lost = status == "not_joined" or (
            status == "inaccessible" and error in self._MEMBERSHIP_LOST_ERRORS
        )

        if chat_id is not None and membership_lost:
            await self.pool.execute(
                """UPDATE chats SET membership_status='left',last_scanned=NOW()
                   WHERE chat_id=$1 AND membership_status='joined'""",
                chat_id,
            )
        return status
