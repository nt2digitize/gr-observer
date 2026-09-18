"""Durable bridge for Two Screens and Live inside the linear PV conversation.

This module reuses the existing Two Screens and Live tables. It does not create a
second conversation engine, Telegram client, Writer or queue. The linear lane
remains the sole progression authority; capabilities only hold their own facts
and may enqueue work into the existing Outbox.
"""

from __future__ import annotations

import json
from decimal import Decimal

from .pv_linear_flow import LINEAR_POSITION_GAP

CAPABILITY_MIGRATION_VERSION = 5
TWO_SCREENS_PROMPT_STEP_KEY = "two.prompt"
TWO_SCREENS_CHOICE_STEP_KEY = "two.prompt.preference"
LIVE_OPTIN_STEP_KEY = "live.optin"
CAPABILITY_STEP_KEYS = (
    TWO_SCREENS_PROMPT_STEP_KEY,
    TWO_SCREENS_CHOICE_STEP_KEY,
    LIVE_OPTIN_STEP_KEY,
)


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class PvLinearCapabilityStore:
    """Reuse existing feature state while keeping linear progression separate."""

    def __init__(self, pool):
        self.pool = pool
        self._ready = False

    async def ensure_ready(self) -> None:
        """Insert capability balloons after the preview link exactly once."""
        if self._ready:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                applied = await conn.fetchval(
                    "SELECT 1 FROM pv_message_step_migrations WHERE version=$1",
                    CAPABILITY_MIGRATION_VERSION,
                )
                if not applied:
                    preview_position = await conn.fetchval(
                        """SELECT linear_position FROM pv_message_steps
                           WHERE step_key='link.preview' AND linear_enabled IS TRUE"""
                    )
                    if preview_position is None:
                        raise RuntimeError("preview linear ausente")
                    next_position = await conn.fetchval(
                        """SELECT linear_position FROM pv_message_steps
                           WHERE linear_enabled IS TRUE AND linear_position>$1
                           ORDER BY linear_position,id LIMIT 1""",
                        preview_position,
                    )
                    start = Decimal(preview_position)
                    end = (
                        Decimal(next_position)
                        if next_position is not None
                        else start + (LINEAR_POSITION_GAP * 4)
                    )
                    if end <= start:
                        end = start + (LINEAR_POSITION_GAP * 4)
                    gap = (end - start) / Decimal(4)
                    planned = (
                        (TWO_SCREENS_PROMPT_STEP_KEY, start + gap, False),
                        (TWO_SCREENS_CHOICE_STEP_KEY, start + (gap * 2), True),
                        (LIVE_OPTIN_STEP_KEY, start + (gap * 3), True),
                    )
                    for step_key, position, wait_for_reply in planned:
                        updated = await conn.fetchval(
                            """UPDATE pv_message_steps
                               SET phase_key='warmup',linear_position=$2,
                                   wait_for_reply=$3,linear_enabled=TRUE,updated_at=NOW()
                               WHERE step_key=$1 RETURNING id""",
                            step_key,
                            position,
                            wait_for_reply,
                        )
                        if updated is None:
                            raise RuntimeError(f"capability step ausente: {step_key}")
                    await conn.execute(
                        """INSERT INTO pv_message_step_migrations(version)
                           VALUES($1) ON CONFLICT(version) DO NOTHING""",
                        CAPABILITY_MIGRATION_VERSION,
                    )
        self._ready = True

    async def waiting_step(self, user_id: int):
        await self.ensure_ready()
        return await self.pool.fetchrow(
            """SELECT s.user_id,s.status,s.current_step_id,s.current_position,
                      s.generation,m.step_key
               FROM pv_linear_sessions s
               LEFT JOIN pv_message_steps m ON m.id=s.current_step_id
               WHERE s.user_id=$1""",
            int(user_id),
        )

    async def waiting_on(self, user_id: int, step_key: str) -> bool:
        row = await self.waiting_step(user_id)
        return bool(
            row is not None
            and str(row["status"]) == "waiting_reply"
            and str(row["step_key"] or "") == str(step_key)
        )

    async def prepare_two_screens(self, user_id: int) -> str:
        """Create only the compatibility state required by the existing photo store."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Linear mode does not use the legacy contact state machine. A completed
                # shell prevents old PV progression while satisfying the existing FK.
                await conn.execute(
                    """INSERT INTO pv_reply_contacts(user_id,stage,last_inbound_at,updated_at)
                       VALUES($1,'completed',NOW(),NOW())
                       ON CONFLICT(user_id) DO NOTHING""",
                    int(user_id),
                )
                status = await conn.fetchval(
                    """SELECT status FROM pv_two_screens_sessions
                       WHERE user_id=$1 FOR UPDATE""",
                    int(user_id),
                )
                if status is None:
                    await conn.execute(
                        """INSERT INTO pv_two_screens_sessions(user_id,status)
                           VALUES($1,'prompt_queued')""",
                        int(user_id),
                    )
                    return "ready"
                status = str(status)
                if status == "prompt_queued":
                    return "ready"
                if status in {"completed", "stopped"}:
                    return "already_done"
                # Never steal a partially-running legacy session at cutover. Skipping
                # it is safer than duplicating media or reopening an old choice.
                return "legacy_active"

    async def complete_two_screens(self, user_id: int) -> None:
        await self.pool.execute(
            """UPDATE pv_two_screens_sessions
               SET status='completed',completed_at=COALESCE(completed_at,NOW()),updated_at=NOW()
               WHERE user_id=$1 AND status NOT IN ('completed','stopped')""",
            int(user_id),
        )

    async def handle_live_optin_response(self, user_id: int, response_kind: str) -> str:
        """Resolve only the pending subscription question; never advance the line."""
        if response_kind not in {"positive", "negative"}:
            return "waiting"
        status = "subscribed" if response_kind == "positive" else "declined"
        updated = await self.pool.fetchval(
            """UPDATE live_alert_subscriptions
               SET status=$2,responded_at=NOW(),updated_at=NOW()
               WHERE user_id=$1 AND status='pending' RETURNING user_id""",
            int(user_id),
            status,
        )
        return status if updated is not None else "not_pending"

    async def handle_active_live_response(self, user_id: int, response_kind: str) -> str | None:
        """Consume a reply to an active Live invite without touching linear state."""
        if response_kind not in {"positive", "negative"}:
            return None
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                active = await conn.fetchrow(
                    """SELECT campaign_id,stage FROM live_campaign_recipients
                       WHERE user_id=$1 AND stage IN ('awaiting','remarketing_sent')
                       ORDER BY campaign_id DESC LIMIT 1 FOR UPDATE""",
                    int(user_id),
                )
                if active is None:
                    return None
                campaign_id = int(active["campaign_id"])
                if response_kind == "negative":
                    await conn.execute(
                        """UPDATE live_campaign_recipients
                           SET stage='stopped',updated_at=NOW()
                           WHERE campaign_id=$1 AND user_id=$2""",
                        campaign_id,
                        int(user_id),
                    )
                    return "live_declined"
                await conn.execute(
                    """UPDATE live_campaign_recipients
                       SET stage='link_queued',updated_at=NOW()
                       WHERE campaign_id=$1 AND user_id=$2""",
                    campaign_id,
                    int(user_id),
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(
                         action_key,module_id,action_type,payload)
                       VALUES($1,'pv_reply','send_live_link',$2::jsonb)
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"pv_reply:live-link:{campaign_id}:{int(user_id)}",
                    _json({"peer": int(user_id), "campaign_id": campaign_id}),
                )
                return "live_link_queued"
