"""Outbox claim policy with priority, aging and per-lane fairness.

This is still the same persistence boundary and the same single Writer. Only the
atomic choice of the next *ready* action changes; availability times, pacing and
TrafficGovernor cooldowns remain authoritative.
"""

from __future__ import annotations

from typing import Any

from .pv_suppression import ensure_schema as ensure_suppression_schema
from .pv_suppression import is_suppressed
from .queue_policy import (
    AGING_STEP_SECONDS,
    LANE_REPEAT_PENALTY,
    P00_GROUP_CAPTURE,
    lane_expr_sql,
    priority_case_sql,
)
from .storage import Storage, _decoded

SCHEDULER_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox_scheduler_state (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
  last_lane_key TEXT,
  last_action_id BIGINT,
  last_claimed_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO outbox_scheduler_state(singleton) VALUES(TRUE)
ON CONFLICT(singleton) DO NOTHING;
"""


class PriorityStorage(Storage):
    """Canonical Storage with a fair atomic ready-action selector."""

    async def initialize(self) -> None:
        await super().initialize()
        await self.pool.execute(SCHEDULER_SCHEMA)
        await ensure_suppression_schema(self.pool)

    async def accept_pv_message(self, **kwargs) -> str:
        """Do not advance or enqueue a PV journey while the admin kill switch is on."""
        user_id = int(kwargs["user_id"])
        if await is_suppressed(self.pool, user_id):
            return "suppressed"
        return await super().accept_pv_message(**kwargs)

    async def claim_next_action(self) -> dict[str, Any] | None:
        priority_sql = priority_case_sql("actions")
        lane_sql = lane_expr_sql("actions")
        age_steps_sql = (
            "GREATEST(0,FLOOR(EXTRACT(EPOCH FROM "
            f"(NOW()-actions.available_at))/{AGING_STEP_SECONDS}))::integer"
        )
        normal_score_sql = (
            f"GREATEST(0,({priority_sql})-LEAST(({priority_sql}),{age_steps_sql}))"
            f" + CASE WHEN ({lane_sql})=$1 THEN {LANE_REPEAT_PENALTY} ELSE 0 END"
        )
        # P00 is semantic, not an aging destination. Preserve it exactly in the
        # real PostgreSQL selector and do not let the one-turn lane penalty turn
        # a ready capture into a P0 tie. All ordinary work still ages only to P0.
        score_sql = (
            f"CASE WHEN ({priority_sql})={P00_GROUP_CAPTURE} "
            f"THEN {P00_GROUP_CAPTURE} ELSE ({normal_score_sql}) END"
        )

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # A stop command may race with future schedulers that already
                # persisted work. Neutralize it without deleting audit history.
                await conn.execute(
                    """UPDATE outbox_actions actions SET status='succeeded',
                         result=jsonb_build_object(
                           'sent',FALSE,'reason','admin_suppressed',
                           'peer',actions.payload->>'peer'
                         ),lease_until=NULL,last_error=NULL,updated_at=NOW()
                       WHERE actions.module_id='pv_reply'
                         AND actions.status='pending'
                         AND actions.payload ? 'peer'
                         AND EXISTS(
                           SELECT 1 FROM pv_suppressed_users suppressed
                           WHERE suppressed.active IS TRUE
                             AND suppressed.user_id::text=actions.payload->>'peer'
                         )"""
                )
                last_lane = await conn.fetchval(
                    """SELECT last_lane_key FROM outbox_scheduler_state
                       WHERE singleton=TRUE FOR UPDATE"""
                )
                candidate = await conn.fetchrow(
                    f"""SELECT actions.id, {lane_sql} AS lane_key
                         FROM outbox_actions actions
                         JOIN module_control modules
                           ON modules.module_id=actions.module_id
                          AND modules.enabled IS TRUE
                         WHERE actions.status='pending'
                           AND actions.available_at<=NOW()
                           AND NOT (
                             actions.module_id='pv_reply'
                             AND actions.payload ? 'peer'
                             AND EXISTS(
                               SELECT 1 FROM pv_suppressed_users suppressed
                               WHERE suppressed.active IS TRUE
                                 AND suppressed.user_id::text=actions.payload->>'peer'
                             )
                           )
                         ORDER BY {score_sql},
                                  actions.available_at,
                                  actions.id
                         FOR UPDATE OF actions SKIP LOCKED
                         LIMIT 1""",
                    last_lane,
                )
                if candidate is None:
                    return None

                row = await conn.fetchrow(
                    """UPDATE outbox_actions
                       SET status='processing',attempts=attempts+1,
                           lease_until=NOW()+INTERVAL '30 minutes',updated_at=NOW()
                       WHERE id=$1 AND status='pending'
                       RETURNING *""",
                    int(candidate["id"]),
                )
                if row is None:
                    return None

                await conn.execute(
                    """UPDATE outbox_scheduler_state
                       SET last_lane_key=$1,last_action_id=$2,
                           last_claimed_at=NOW(),updated_at=NOW()
                       WHERE singleton=TRUE""",
                    str(candidate["lane_key"]),
                    int(candidate["id"]),
                )

        result = dict(row)
        result["payload"] = _decoded(result["payload"]) or {}
        return result
