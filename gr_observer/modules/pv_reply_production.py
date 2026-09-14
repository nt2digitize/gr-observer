"""Canonical production policy for the PV rib.

All safeguards live in normal class composition. Nothing in this module replaces
methods on existing objects at runtime.
"""

from __future__ import annotations

import asyncio
import logging

from ..human_timing import MIN_WRITING_DELAY_SECONDS
from ..pv_message_steps import POSITION_GAP
from .pv_reply_contacts import PvReplyWithContacts

log = logging.getLogger("gr-observer.pv-production")


class PvReplyProduction(PvReplyWithContacts):
    """Production PV rib with per-contact ordering and stale-step protection."""

    _LOCK_STRIPES = 256

    def __init__(self, storage, settings):
        super().__init__(storage, settings)
        self._accept_locks = [asyncio.Lock() for _ in range(self._LOCK_STRIPES)]

    async def on_connect(self, client, me) -> None:
        await super().on_connect(client, me)
        restored = await self._restore_missing_required_destinations()
        if restored:
            log.warning("PV restored missing required steps: %s", ",".join(restored))

    async def handle_event(self, event) -> bool:
        if event.is_private and not event.out and event.sender_id:
            lock = self._accept_locks[int(event.sender_id) % self._LOCK_STRIPES]
            async with lock:
                return await super().handle_event(event)
        return await super().handle_event(event)

    async def _restore_missing_required_destinations(self) -> tuple[str, ...]:
        """Restore only required rows physically deleted by old editor behavior."""
        reply_delay = max(MIN_WRITING_DELAY_SECONDS, int(self.settings.pv_reply_delay_seconds))
        rows = (
            ("link.preview", "link", POSITION_GAP * 2, "Link da prévia", "{preview_link}", 7),
            ("followup.link", "followup", POSITION_GAP * 2, "Link do follow-up", "{preview_link}", 3),
            ("reminder.link", "reminder_link", POSITION_GAP, "Reenvio do link", "{preview_link}", reply_delay),
            ("weekly.link1", "weekly", POSITION_GAP * 2, "Link semanal 1", "{preview_link}", 7),
            ("weekly.link2", "weekly", POSITION_GAP * 4, "Link semanal 2", "{preview_link}", 3),
            # Destination-pair migration owns position 1; the actual destination is position 2.
            ("live.link", "live_link", POSITION_GAP * 2, "Destino", "{live_link}", MIN_WRITING_DELAY_SECONDS),
        )
        restored: list[str] = []
        for step_key, block_key, position, label, content, median in rows:
            inserted = await self.storage.pool.fetchval(
                """INSERT INTO pv_message_steps(
                       step_key,block_key,branch_key,position,label,content,kind,
                       median_delay_seconds,variant_root,built_in,updated_at)
                   VALUES($1,$2,NULL,$3,$4,$5,'link',$6,FALSE,TRUE,NOW())
                   ON CONFLICT(step_key) DO NOTHING RETURNING step_key""",
                step_key,
                block_key,
                position,
                label,
                content,
                max(MIN_WRITING_DELAY_SECONDS, int(median)),
            )
            if inserted:
                restored.append(str(inserted))
        return tuple(restored)

    async def _queue_sequence_continuation(self, **kwargs) -> bool:
        peer = int(kwargs["peer"])
        row = await self.storage.pool.fetchrow(
            "SELECT stage,last_inbound_message_id FROM pv_reply_contacts WHERE user_id=$1",
            peer,
        )
        context = dict(kwargs.get("context") or {})
        context["_pv_guard"] = {
            "stage": str(row["stage"]) if row is not None else None,
            "last_inbound_message_id": (
                int(row["last_inbound_message_id"])
                if row is not None and row["last_inbound_message_id"] is not None
                else None
            ),
        }
        kwargs["context"] = context
        return await super()._queue_sequence_continuation(**kwargs)

    async def action_send_message_step(self, action: dict, effects) -> dict:
        payload = action.get("payload") or {}
        context = dict(payload.get("context") or {})
        guard = context.get("_pv_guard")
        if not isinstance(guard, dict):
            return {"sent": False, "reason": "legacy_step_quarantined"}

        peer = int(payload.get("peer") or 0)
        row = await self.storage.pool.fetchrow(
            "SELECT stage,last_inbound_message_id FROM pv_reply_contacts WHERE user_id=$1",
            peer,
        )
        current_stage = str(row["stage"]) if row is not None else None
        current_message_id = (
            int(row["last_inbound_message_id"])
            if row is not None and row["last_inbound_message_id"] is not None
            else None
        )
        expected_message_id = guard.get("last_inbound_message_id")
        if expected_message_id is not None:
            expected_message_id = int(expected_message_id)
        if current_stage != guard.get("stage") or current_message_id != expected_message_id:
            return {"sent": False, "reason": "conversation_state_changed"}
        return await super().action_send_message_step(action, effects)
