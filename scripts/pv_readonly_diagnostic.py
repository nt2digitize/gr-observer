"""Read-only operational snapshot for one PV lead.

This tool is deliberately outside the application runtime. It opens PostgreSQL
in read-only mode, performs SELECT queries only, prints a sanitized JSON report,
and exits. It has no Telegram client, no USER session, no Writer and no Outbox
mutation path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime
from decimal import Decimal
from typing import Any

import asyncpg


def _json_default(value: Any):
    if isinstance(value, (datetime, Decimal)):
        return str(value)
    raise TypeError(type(value).__name__)


def _rows(rows) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


async def snapshot(database_url: str, peer: int) -> dict[str, Any]:
    conn = await asyncpg.connect(
        database_url,
        server_settings={"default_transaction_read_only": "on"},
    )
    try:
        async with conn.transaction(readonly=True):
            session = await conn.fetch(
                """SELECT s.user_id,s.status,s.current_step_id,s.current_position,
                          s.generation,s.last_inbound_message_id,s.created_at,s.updated_at,
                          m.step_key,m.label,m.median_delay_seconds,m.wait_for_reply,
                          m.linear_enabled,m.linear_position
                   FROM pv_linear_sessions s
                   LEFT JOIN pv_message_steps m ON m.id=s.current_step_id
                   WHERE s.user_id=$1""",
                peer,
            )
            next_step = await conn.fetch(
                """SELECT id,step_key,label,median_delay_seconds,wait_for_reply,
                          linear_enabled,linear_position
                   FROM pv_message_steps
                   WHERE linear_enabled IS TRUE
                     AND linear_position>(
                       SELECT current_position FROM pv_linear_sessions WHERE user_id=$1
                     )
                   ORDER BY linear_position,id LIMIT 1""",
                peer,
            )
            outbox = await conn.fetch(
                """SELECT id,action_key,action_type,status,attempts,available_at,
                          lease_until,last_error,created_at,updated_at
                   FROM outbox_actions
                   WHERE module_id='pv_reply'
                     AND payload->>'peer'=$1
                   ORDER BY id DESC LIMIT 20""",
                str(peer),
            )
            effects = await conn.fetch(
                """SELECT effect_key,effect_type,status,attempts,last_error,
                          created_at,updated_at
                   FROM telegram_effects
                   WHERE payload->>'peer'=$1 OR payload->>'destination_peer'=$1
                   ORDER BY updated_at DESC LIMIT 20""",
                str(peer),
            )
            live = await conn.fetch(
                """SELECT user_id,status,asked_at,responded_at,updated_at
                   FROM live_alert_subscriptions WHERE user_id=$1""",
                peer,
            )
            two_screens = await conn.fetch(
                """SELECT user_id,status,selected_slot,choice_retry_sent,
                          updated_at,completed_at
                   FROM pv_two_screens_sessions WHERE user_id=$1""",
                peer,
            )
            membership = await conn.fetch(
                """SELECT user_id,status,join_count,leave_count,last_change_at,updated_at
                   FROM pv_group_membership_state
                   WHERE user_id=$1 AND preview_group IS TRUE
                   ORDER BY updated_at DESC LIMIT 5""",
                peer,
            )
        return {
            "kind": "pv_readonly_diagnostic",
            "peer": peer,
            "session": _rows(session),
            "next_step": _rows(next_step),
            "outbox": _rows(outbox),
            "effects": _rows(effects),
            "live": _rows(live),
            "two_screens": _rows(two_screens),
            "membership": _rows(membership),
        }
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="PV read-only diagnostic")
    parser.add_argument("peer", type=int, help="Telegram user_id (>0)")
    args = parser.parse_args()
    if args.peer <= 0:
        raise SystemExit("peer must be positive")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    report = asyncio.run(snapshot(database_url, args.peer))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=_json_default))


if __name__ == "__main__":
    main()
