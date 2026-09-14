"""Outbox claim policy with priority, aging and per-lane fairness.

This is still the same persistence boundary and the same single Writer. Only the
atomic choice of the next *ready* action changes; availability times, pacing and
TrafficGovernor cooldowns remain authoritative.
"""

from __future__ import annotations

from typing import Any

from .queue_policy import (
    AGING_STEP_SECONDS,
    LANE_REPEAT_PENALTY,
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

    async def claim_next_action(self) -> dict[str, Any] | None:
        priority_sql = priority_case_sql("actions")
        lane_sql = lane_expr_sql("actions")
        age_steps_sql = (
            "GREATEST(0,FLOOR(EXTRACT(EPOCH FROM "
            f"(NOW()-actions.available_at))/{AGING_STEP_SECONDS}))::integer"
        )
        score_sql = (
            f"GREATEST(0,({priority_sql})-LEAST(({priority_sql}),{age_steps_sql}))"
            f" + CASE WHEN ({lane_sql})=$1 THEN {LANE_REPEAT_PENALTY} ELSE 0 END"
        )

        async with self.pool.acquire() as conn:
            async with conn.transaction():
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
                         ORDER BY {score_sql},
                                  ({priority_sql}),
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
