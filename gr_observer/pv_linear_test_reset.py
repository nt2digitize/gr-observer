"""One-shot, fail-closed reset for a scoped linear-PV homologation lead.

This helper never creates Telegram clients, writers or queues. It only re-arms the
existing durable linear session for explicitly allowlisted test users so the next
inbound PV starts a new generation from the first balloon. A reset token makes the
operation idempotent across restarts/deploy retries.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

import asyncpg

log = logging.getLogger("gr-observer.pv-linear-test-reset")

_RESET_SCHEMA = """
CREATE TABLE IF NOT EXISTS pv_linear_test_resets (
    reset_token TEXT NOT NULL,
    user_id BIGINT NOT NULL,
    applied BOOLEAN NOT NULL DEFAULT FALSE,
    previous_generation BIGINT,
    new_generation BIGINT,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY(reset_token,user_id)
);
"""


def _parse_ids(raw: str | None) -> frozenset[int]:
    if not raw or not raw.strip():
        return frozenset()
    values: set[int] = set()
    normalized = raw.replace(";", ",").replace(" ", ",")
    for token in normalized.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            value = int(token)
        except (TypeError, ValueError):
            continue
        if value > 0:
            values.add(value)
    return frozenset(values)


def configured_reset_request(
    environ: Mapping[str, str] | None = None,
) -> tuple[str, tuple[int, ...]] | None:
    """Return a reset only when every target is inside the active test allowlist."""
    env = os.environ if environ is None else environ
    token = str(env.get("PV_LINEAR_TEST_RESET_TOKEN") or "").strip()
    requested = _parse_ids(env.get("PV_LINEAR_TEST_RESET_USER_IDS"))
    allowed = _parse_ids(env.get("PV_LINEAR_TEST_USER_IDS"))
    if not token or not requested or not allowed:
        return None
    if not requested.issubset(allowed):
        log.warning("PV linear test reset recusado: alvo fora do escopo de homologação")
        return None
    return token, tuple(sorted(requested))


async def apply_linear_test_resets(*, connect=None) -> int:
    """Arm each requested test lane once; the next inbound starts at position zero."""
    request = configured_reset_request()
    if request is None:
        return 0
    database_url = str(os.getenv("DATABASE_URL") or "").strip()
    if not database_url:
        log.warning("PV linear test reset ignorado: DATABASE_URL ausente")
        return 0

    token, user_ids = request
    connector = asyncpg.connect if connect is None else connect
    conn = None
    applied_count = 0
    try:
        conn = await connector(database_url)
        await conn.execute(_RESET_SCHEMA)
        for user_id in user_ids:
            async with conn.transaction():
                already = await conn.fetchval(
                    """SELECT 1 FROM pv_linear_test_resets
                       WHERE reset_token=$1 AND user_id=$2""",
                    token,
                    int(user_id),
                )
                if already:
                    continue
                session = await conn.fetchrow(
                    """SELECT generation,status,current_position
                       FROM pv_linear_sessions WHERE user_id=$1 FOR UPDATE""",
                    int(user_id),
                )
                if session is None:
                    await conn.execute(
                        """INSERT INTO pv_linear_test_resets(
                             reset_token,user_id,applied,previous_generation,new_generation)
                           VALUES($1,$2,FALSE,NULL,NULL)""",
                        token,
                        int(user_id),
                    )
                    log.warning(
                        "PV linear test reset sem sessão peer=%s token=%s",
                        int(user_id),
                        token,
                    )
                    continue

                previous_generation = int(session["generation"])
                new_generation = await conn.fetchval(
                    """UPDATE pv_linear_sessions
                       SET status='waiting_reply',current_step_id=NULL,current_position=0,
                           generation=generation+1,last_inbound_message_id=NULL,
                           updated_at=NOW()
                       WHERE user_id=$1 RETURNING generation""",
                    int(user_id),
                )
                await conn.execute(
                    """INSERT INTO pv_linear_test_resets(
                         reset_token,user_id,applied,previous_generation,new_generation)
                       VALUES($1,$2,TRUE,$3,$4)""",
                    token,
                    int(user_id),
                    previous_generation,
                    int(new_generation),
                )
                applied_count += 1
                log.info(
                    "PV linear test reset aplicado peer=%s generation=%s->%s token=%s",
                    int(user_id),
                    previous_generation,
                    int(new_generation),
                    token,
                )
    except Exception as exc:
        log.warning("PV linear test reset falhou erro=%s", type(exc).__name__)
        return applied_count
    finally:
        if conn is not None:
            await conn.close()
    return applied_count
