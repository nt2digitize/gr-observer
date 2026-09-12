"""Durable scheduling rules for the autonomous PV journey.

This contract owns only PostgreSQL state transitions and Outbox intents. It does
not send Telegram messages directly; every mutation still crosses the single
Writer and the existing TelegramEffects gateway.
"""

from __future__ import annotations

import json


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class PvJourneyStore:
    """Persistence contract for non-blocking PV progression."""

    def __init__(self, pool):
        self.pool = pool

    async def contact_allows_automation(self, user_id: int) -> bool:
        stage = await self.pool.fetchval(
            "SELECT stage FROM pv_reply_contacts WHERE user_id=$1",
            user_id,
        )
        return stage is not None and str(stage) != "stopped"

    async def greeting_sent_and_schedule_link(
        self, *, user_id: int, delay_seconds: int
    ) -> bool:
        """Advance greeting -> link atomically without waiting for a reply."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                updated = await conn.fetchval(
                    """UPDATE pv_reply_contacts
                       SET stage='link_queued',greeting_sent_at=NOW(),
                           link_queued_at=NOW(),updated_at=NOW()
                       WHERE user_id=$1 AND stage='greeting_queued'
                       RETURNING user_id""",
                    user_id,
                )
                if updated is None:
                    return False
                await conn.execute(
                    """INSERT INTO outbox_actions(
                       action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply','send_link',$2::jsonb,
                         NOW()+($3::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:auto-link:{user_id}",
                    _json({"peer": user_id, "campaign_id": "pv.preview_link"}),
                    max(0, int(delay_seconds)),
                )
                return True

    async def ensure_two_screens_after_link(
        self, *, user_id: int, delay_seconds: int
    ) -> bool:
        """Start the photo branch when at least one catalogued slot exists.

        Storage's older link transaction requires all three slots. This
        idempotent reconciler broadens the invariant to "at least one usable
        catalogue row" without duplicating sessions/actions already created by
        the older path.
        """
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                stage = await conn.fetchval(
                    "SELECT stage FROM pv_reply_contacts WHERE user_id=$1 FOR UPDATE",
                    user_id,
                )
                if stage is None or str(stage) == "stopped":
                    return False
                media_ready = await conn.fetchval(
                    """SELECT EXISTS(
                       SELECT 1 FROM pv_two_screens_media_slots
                       WHERE slot IN ('peitos','buceta','cu'))"""
                )
                if not media_ready:
                    return False
                inserted = await conn.fetchval(
                    """INSERT INTO pv_two_screens_sessions(user_id,status)
                       VALUES($1,'prompt_queued') ON CONFLICT(user_id) DO NOTHING
                       RETURNING user_id""",
                    user_id,
                )
                if inserted is None:
                    return False
                await conn.execute(
                    """INSERT INTO outbox_actions(
                       action_key,module_id,action_type,payload,available_at)
                       VALUES($1,'pv_reply','send_two_screens_prompt',$2::jsonb,
                         NOW()+($3::double precision*INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:two-screens:prompt:{user_id}",
                    _json({"peer": user_id}),
                    max(0, int(delay_seconds)),
                )
                return True
