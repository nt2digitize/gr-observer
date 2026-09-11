"""Persistence boundary, including transactional inbox/outbox operations."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .schema import SCHEMA


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _decoded(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def run_id_for(trigger_key: str) -> str:
    return hashlib.sha256(trigger_key.encode("utf-8")).hexdigest()[:24]


class Storage:
    def __init__(self, pool):
        self.pool = pool

    async def initialize(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def recover_interrupted(self) -> None:
        """Quarantine work left between database and Telegram by a dead process."""
        await self.pool.execute(
            """UPDATE outbox_actions SET status='review', lease_until=NULL,
               last_error=COALESCE(last_error,'Processo interrompido; revisão necessária'),
               updated_at=NOW() WHERE status='processing'"""
        )
        await self.pool.execute(
            """UPDATE telegram_effects SET status='review',
               last_error=COALESCE(last_error,'Efeito interrompido; reconciliar antes de repetir'),
               updated_at=NOW() WHERE status='processing'"""
        )
        await self.pool.execute(
            """UPDATE module_runs SET status='review', finished_at=NOW(),
               error=COALESCE(error,'Execução interrompida; revisão necessária')
               WHERE status='running'"""
        )

    async def module_states(self) -> dict[str, dict[str, Any]]:
        rows = await self.pool.fetch(
            "SELECT module_id, enabled, reason, config, updated_at FROM module_control ORDER BY module_id"
        )
        return {
            row["module_id"]: {
                "enabled": row["enabled"],
                "reason": row["reason"],
                "config": _decoded(row["config"]) or {},
                "updated_at": row["updated_at"],
            }
            for row in rows
        }

    async def set_module_state(
        self, module_id: str, enabled: bool, reason: str
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO module_control(module_id,enabled,reason,updated_at)
                       VALUES($1,$2,$3,NOW()) ON CONFLICT(module_id) DO UPDATE SET
                       enabled=EXCLUDED.enabled, reason=EXCLUDED.reason, updated_at=NOW()""",
                    module_id,
                    enabled,
                    reason,
                )
                if module_id == "radar":
                    await conn.execute(
                        """UPDATE observer_control SET enabled=$1, reason=$2, updated_at=NOW()
                           WHERE singleton=TRUE""",
                        enabled,
                        reason,
                    )

    async def set_module_reason(self, module_id: str, reason: str) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "UPDATE module_control SET reason=$2, updated_at=NOW() WHERE module_id=$1",
                    module_id,
                    reason,
                )
                if module_id == "radar":
                    await conn.execute(
                        """UPDATE observer_control SET reason=$1, updated_at=NOW()
                           WHERE singleton=TRUE""",
                        reason,
                    )

    async def stored_controller_id(self) -> int | None:
        raw = await self.pool.fetchval(
            "SELECT config->>'controller_id' FROM module_control WHERE module_id='botson'"
        )
        return int(raw) if raw and str(raw).lstrip("-").isdigit() else None

    async def save_controller_id(self, controller_id: int) -> None:
        await self.pool.execute(
            """UPDATE module_control SET
               config=jsonb_set(config,'{controller_id}',to_jsonb($1::bigint),TRUE),
               updated_at=NOW() WHERE module_id='botson'""",
            controller_id,
        )

    async def pair_and_enqueue(
        self,
        *,
        event_key: str,
        controller_id: int,
        reply_peer: int,
        reply_text: str,
    ) -> str:
        """Persist pairing and its Telegram acknowledgement in one transaction."""
        action_key = f"botson:reply:{event_key}:paired"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO inbox_events(source,event_key,module_id,payload)
                       VALUES('telegram-user',$1,'botson',$2::jsonb)
                       ON CONFLICT DO NOTHING RETURNING event_key""",
                    event_key,
                    _json({"kind": "pair", "sender_id": controller_id}),
                )
                if not inserted:
                    return "duplicate"
                await conn.execute(
                    """UPDATE module_control SET
                       config=jsonb_set(config,'{controller_id}',to_jsonb($1::bigint),TRUE),
                       updated_at=NOW() WHERE module_id='botson'""",
                    controller_id,
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                       VALUES($1,'botson','reply',$2::jsonb)
                       ON CONFLICT(action_key) DO NOTHING""",
                    action_key,
                    _json({"peer": reply_peer, "text": reply_text}),
                )
        return "accepted"

    async def accept_and_enqueue(
        self,
        *,
        source: str,
        event_key: str,
        module_id: str,
        event_payload: dict[str, Any],
        action_type: str,
        action_payload: dict[str, Any],
        exclusive: bool = False,
    ) -> str:
        """Persist input and action intent atomically.

        Returns ``accepted``, ``duplicate`` or ``busy``.  This transaction is
        the Dual Write boundary: no Telegram call occurs here.
        """
        action_key = f"{module_id}:{action_type}:{source}:{event_key}"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO inbox_events(source,event_key,module_id,payload)
                       VALUES($1,$2,$3,$4::jsonb) ON CONFLICT DO NOTHING
                       RETURNING event_key""",
                    source,
                    event_key,
                    module_id,
                    _json(event_payload),
                )
                if not inserted:
                    return "duplicate"
                if exclusive:
                    # Lock the module row so two incoming triggers cannot both
                    # pass the open-work test.
                    await conn.fetchval(
                        "SELECT module_id FROM module_control WHERE module_id=$1 FOR UPDATE",
                        module_id,
                    )
                    if module_id == "botson" and action_type in {
                        "run_fleet",
                        "retest_one",
                    }:
                        open_action = await conn.fetchval(
                            """SELECT id FROM outbox_actions WHERE module_id=$1
                               AND action_type IN ('run_fleet','retest_one')
                               AND status IN ('pending','processing') LIMIT 1""",
                            module_id,
                        )
                    else:
                        open_action = await conn.fetchval(
                            """SELECT id FROM outbox_actions WHERE module_id=$1
                               AND action_type=$2 AND status IN ('pending','processing') LIMIT 1""",
                            module_id,
                            action_type,
                        )
                    if open_action:
                        return "busy"
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                       VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(action_key) DO NOTHING""",
                    action_key,
                    module_id,
                    action_type,
                    _json(action_payload),
                )
                if action_type in {"run_fleet", "retest_one"}:
                    run_id = run_id_for(action_key)
                    await conn.execute(
                        """INSERT INTO module_runs(run_id,module_id,trigger_key,status,request)
                           VALUES($1,$2,$3,'queued',$4::jsonb)
                           ON CONFLICT(trigger_key) DO NOTHING""",
                        run_id,
                        module_id,
                        action_key,
                        _json(action_payload),
                    )
        return "accepted"

    async def enqueue_action(
        self,
        *,
        action_key: str,
        module_id: str,
        action_type: str,
        payload: dict[str, Any],
    ) -> bool:
        inserted = await self.pool.fetchval(
            """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
               VALUES($1,$2,$3,$4::jsonb) ON CONFLICT(action_key) DO NOTHING
               RETURNING id""",
            action_key,
            module_id,
            action_type,
            _json(payload),
        )
        return inserted is not None

    async def claim_next_action(self) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """UPDATE outbox_actions SET status='processing', attempts=attempts+1,
               lease_until=NOW()+INTERVAL '30 minutes', updated_at=NOW()
               WHERE id=(SELECT id FROM outbox_actions WHERE status='pending'
                         ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1)
               RETURNING *"""
        )
        if not row:
            return None
        result = dict(row)
        result["payload"] = _decoded(result["payload"]) or {}
        return result

    async def finish_action(self, action_id: int, result: Any = None) -> None:
        await self.pool.execute(
            """UPDATE outbox_actions SET status='succeeded', result=$2::jsonb,
               lease_until=NULL, updated_at=NOW() WHERE id=$1""",
            action_id,
            _json(result or {}),
        )

    async def fail_action(self, action_id: int, exc: BaseException) -> None:
        await self.pool.execute(
            """UPDATE outbox_actions SET status='failed', last_error=$2,
               lease_until=NULL, updated_at=NOW() WHERE id=$1""",
            action_id,
            f"{type(exc).__name__}: {exc}"[:1000],
        )

    async def review_action(self, action_id: int, exc: BaseException) -> None:
        await self.pool.execute(
            """UPDATE outbox_actions SET status='review', last_error=$2,
               lease_until=NULL, updated_at=NOW() WHERE id=$1""",
            action_id,
            f"{type(exc).__name__}: {exc}"[:1000],
        )

    async def start_run(self, run_id: str) -> None:
        await self.pool.execute(
            "UPDATE module_runs SET status='running', started_at=NOW() WHERE run_id=$1",
            run_id,
        )

    async def finish_run(self, run_id: str, result: dict[str, Any]) -> None:
        await self.pool.execute(
            """UPDATE module_runs SET status='succeeded', result=$2::jsonb,
               finished_at=NOW() WHERE run_id=$1""",
            run_id,
            _json(result),
        )

    async def finish_run_and_enqueue_reply(
        self,
        *,
        run_id: str,
        result: dict[str, Any],
        reply_peer: int,
        reply_text: str,
    ) -> None:
        """Commit the test result and final delivery intent atomically."""
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE module_runs SET status='succeeded', result=$2::jsonb,
                       finished_at=NOW() WHERE run_id=$1""",
                    run_id,
                    _json(result),
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                       VALUES($1,'botson','reply',$2::jsonb)
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"botson:reply:{run_id}:result",
                    _json({"peer": reply_peer, "text": reply_text}),
                )

    async def fail_run_and_enqueue_reply(
        self,
        *,
        run_id: str,
        exc: BaseException,
        reply_peer: int,
        reply_text: str,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """UPDATE module_runs SET status='failed', error=$2,
                       finished_at=NOW() WHERE run_id=$1""",
                    run_id,
                    f"{type(exc).__name__}: {exc}"[:1000],
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload)
                       VALUES($1,'botson','reply',$2::jsonb)
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"botson:reply:{run_id}:failure",
                    _json({"peer": reply_peer, "text": reply_text}),
                )

    async def fail_run(self, run_id: str, exc: BaseException) -> None:
        await self.pool.execute(
            """UPDATE module_runs SET status='failed', error=$2,
               finished_at=NOW() WHERE run_id=$1""",
            run_id,
            f"{type(exc).__name__}: {exc}"[:1000],
        )

    async def latest_successful_run(self, module_id: str) -> dict[str, Any] | None:
        row = await self.pool.fetchrow(
            """SELECT run_id, result, finished_at FROM module_runs
               WHERE module_id=$1 AND status='succeeded'
               ORDER BY finished_at DESC LIMIT 1""",
            module_id,
        )
        if not row:
            return None
        return {
            "run_id": row["run_id"],
            "result": _decoded(row["result"]),
            "finished_at": row["finished_at"],
        }

    async def begin_effect(
        self,
        *,
        effect_key: str,
        action_id: int,
        effect_type: str,
        payload: dict[str, Any],
    ) -> tuple[str, Any]:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT status,result FROM telegram_effects WHERE effect_key=$1 FOR UPDATE",
                    effect_key,
                )
                if row:
                    if row["status"] == "succeeded":
                        return "succeeded", _decoded(row["result"])
                    return "ambiguous", None
                await conn.execute(
                    """INSERT INTO telegram_effects(
                       effect_key,outbox_action_id,effect_type,payload,status)
                       VALUES($1,$2,$3,$4::jsonb,'processing')""",
                    effect_key,
                    action_id,
                    effect_type,
                    _json(payload),
                )
                return "execute", None

    async def finish_effect(self, effect_key: str, result: Any = None) -> None:
        await self.pool.execute(
            """UPDATE telegram_effects SET status='succeeded', result=$2::jsonb,
               last_error=NULL, updated_at=NOW() WHERE effect_key=$1""",
            effect_key,
            _json(result or {}),
        )

    async def review_effect(self, effect_key: str, exc: BaseException) -> None:
        await self.pool.execute(
            """UPDATE telegram_effects SET status='review', last_error=$2,
               updated_at=NOW() WHERE effect_key=$1""",
            effect_key,
            f"{type(exc).__name__}: {exc}"[:1000],
        )

    async def counts_for_dashboard(self):
        return await self.pool.fetchrow(
            """SELECT COUNT(*) FILTER(WHERE kind='channel') channels,
               COUNT(*) FILTER(WHERE kind='group') groups,
               COUNT(*) FILTER(WHERE can_text IS TRUE) postable,
               COUNT(*) FILTER(WHERE can_text IS NULL OR risk IN ('unknown','medium')) uncertain
               FROM chats"""
        )
