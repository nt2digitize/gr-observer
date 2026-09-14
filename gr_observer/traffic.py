"""First-class traffic governance and passive signal services for the USER spine."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from telethon import errors

from .outbox import (
    AmbiguousExternalEffect,
    DEFINITIVE_RPC_ERRORS,
    DefinitiveExternalEffectError,
    OutboxWriter,
    TelegramEffects,
    _EFFECT_OUTCOME_KEY,
    _EFFECT_REJECTED,
    _rejection_result,
    action_lane,
)
from .telegram_peers import DurablePeerStore, PeerReferenceUnavailable

log = logging.getLogger("gr-observer.traffic")

TELEGRAM_LINK_RE = re.compile(
    r"(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)?[A-Za-z0-9_\-]+(?:\?[^\s<>]+)?",
    re.I,
)
FLOOD_PREFIX = "FloodWaitDeferred:"

TRAFFIC_SCHEMA = """
CREATE TABLE IF NOT EXISTS traffic_cooldowns (
  scope TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  blocked_until TIMESTAMPTZ NOT NULL,
  strikes INTEGER NOT NULL DEFAULT 0,
  last_wait_seconds INTEGER NOT NULL DEFAULT 0,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(scope,scope_key)
);
CREATE INDEX IF NOT EXISTS traffic_cooldowns_until_idx
ON traffic_cooldowns(blocked_until);
CREATE TABLE IF NOT EXISTS traffic_events (
  id BIGSERIAL PRIMARY KEY,
  event_type TEXT NOT NULL,
  effect_type TEXT,
  peer_key TEXT,
  wait_seconds INTEGER,
  action_id BIGINT,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS traffic_events_latest_idx
ON traffic_events(observed_at DESC,id DESC);
CREATE TABLE IF NOT EXISTS system_signals (
  signal_key TEXT PRIMARY KEY,
  signal_type TEXT NOT NULL,
  module_id TEXT NOT NULL DEFAULT 'core',
  actor_id BIGINT,
  chat_id BIGINT,
  message_id BIGINT,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS system_signals_type_time_idx
ON system_signals(signal_type,observed_at DESC);
CREATE INDEX IF NOT EXISTS system_signals_actor_time_idx
ON system_signals(actor_id,observed_at DESC);
"""


class DeferredTraffic(BaseException):
    def __init__(self, wait_seconds: int, reason: str):
        self.wait_seconds = max(1, int(wait_seconds))
        self.reason = reason
        super().__init__(reason)


def telegram_links(text: str) -> tuple[str, ...]:
    found: list[str] = []
    seen: set[str] = set()
    for raw in TELEGRAM_LINK_RE.findall(text or ""):
        value = raw.rstrip(".,);]")
        if value not in seen:
            seen.add(value)
            found.append(value)
    return tuple(found)


class SignalLedger:
    """Neutral facts captured from updates already delivered by Telegram."""

    def __init__(self, pool):
        self.pool = pool
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await self.pool.execute(TRAFFIC_SCHEMA)
                self._ready = True

    async def _put(self, *, signal_key, signal_type, actor_id, chat_id, message_id, payload=None):
        await self.ensure_schema()
        await self.pool.execute(
            """INSERT INTO system_signals(
                 signal_key,signal_type,module_id,actor_id,chat_id,message_id,payload)
               VALUES($1,$2,'core',$3,$4,$5,$6::jsonb)
               ON CONFLICT(signal_key) DO NOTHING""",
            signal_key,
            signal_type,
            actor_id,
            chat_id,
            message_id,
            json.dumps(payload or {}, ensure_ascii=False),
        )

    async def observe_event(self, event, me) -> None:
        message_id = int(getattr(event, "id", 0) or 0) or None
        chat_id = int(getattr(event, "chat_id", 0) or 0) or None
        actor_id = int(getattr(event, "sender_id", 0) or 0) or None
        outgoing = bool(getattr(event, "out", False))
        text = getattr(event, "raw_text", "") or ""
        if outgoing and (getattr(event, "is_group", False) or getattr(event, "is_channel", False)):
            await self._put(
                signal_key=f"manual-post:{chat_id}:{message_id}",
                signal_type="group.manual_post",
                actor_id=actor_id,
                chat_id=chat_id,
                message_id=message_id,
            )
        for index, link in enumerate(telegram_links(text)):
            if getattr(event, "is_private", False) and not outgoing:
                signal_type = "pv.telegram_link_received"
            elif getattr(event, "is_group", False) or getattr(event, "is_channel", False):
                signal_type = "group.telegram_link_seen"
            else:
                signal_type = "telegram.link_seen"
            await self._put(
                signal_key=f"link:{chat_id}:{message_id}:{index}:{link}",
                signal_type=signal_type,
                actor_id=actor_id,
                chat_id=chat_id,
                message_id=message_id,
                payload={"url": link},
            )
        username = str(getattr(me, "username", "") or "").strip().lstrip("@")
        if (
            username
            and not outgoing
            and (getattr(event, "is_group", False) or getattr(event, "is_channel", False))
            and f"@{username.casefold()}" in text.casefold()
        ):
            await self._put(
                signal_key=f"mention:{chat_id}:{message_id}:{actor_id}",
                signal_type="group.mention",
                actor_id=actor_id,
                chat_id=chat_id,
                message_id=message_id,
            )
        if getattr(event, "is_private", False) and not outgoing and actor_id:
            source_chat_id = await self.pool.fetchval(
                """SELECT chat_id FROM system_signals
                   WHERE signal_type='group.mention' AND actor_id=$1
                     AND observed_at > NOW()-INTERVAL '7 days'
                   ORDER BY observed_at DESC LIMIT 1""",
                actor_id,
            )
            if source_chat_id is not None:
                await self.pool.execute(
                    """INSERT INTO private_origins(
                         user_id,username,source_chat_id,confidence,detected_at)
                       VALUES($1,NULL,$2,'probable',NOW())
                       ON CONFLICT(user_id) DO UPDATE SET
                         source_chat_id=EXCLUDED.source_chat_id,
                         confidence='probable',detected_at=NOW()""",
                    actor_id,
                    int(source_chat_id),
                )


class TrafficGovernor:
    """Persistent account/effect/peer cooldowns driven by Telegram feedback."""

    FLOOD_BUFFER_SECONDS = 5

    def __init__(self, pool):
        self.pool = pool
        self._ready = False
        self._lock = asyncio.Lock()

    async def ensure_schema(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await self.pool.execute(TRAFFIC_SCHEMA)
                self._ready = True

    @staticmethod
    def _peer_key(payload: dict[str, Any]) -> str | None:
        value = payload.get("peer")
        if value is None:
            value = payload.get("user_id")
        return None if value is None else str(value)

    async def _remaining(self, scope: str, scope_key: str) -> int:
        await self.ensure_schema()
        value = await self.pool.fetchval(
            """SELECT GREATEST(0,CEIL(EXTRACT(EPOCH FROM (blocked_until-NOW()))))::INTEGER
               FROM traffic_cooldowns WHERE scope=$1 AND scope_key=$2""",
            scope,
            scope_key,
        )
        return max(0, int(value or 0))

    async def account_remaining(self) -> int:
        return await self._remaining("account", "user-session")

    async def guard_or_defer(self, *, action_id: int, effect_type: str, payload: dict[str, Any]) -> None:
        if effect_type == "send_panel_text":
            return
        peer_key = self._peer_key(payload)
        waits = [
            await self._remaining("account", "user-session"),
            await self._remaining("effect", effect_type),
        ]
        if peer_key:
            waits.append(await self._remaining("peer", peer_key))
        remaining = max(waits)
        if remaining <= 0:
            return
        reason = f"TrafficGovernor cooldown {effect_type} ({remaining}s)"
        await self.defer_action(action_id, remaining, reason)
        raise DeferredTraffic(remaining, reason)

    async def defer_action(self, action_id: int, seconds: int, reason: str) -> None:
        await self.pool.execute(
            """UPDATE outbox_actions SET status='pending',
                 available_at=GREATEST(available_at,NOW()+($2*INTERVAL '1 second')),
                 lease_until=NULL,last_error=$3,updated_at=NOW() WHERE id=$1""",
            int(action_id),
            max(1, int(seconds)),
            reason[:1000],
        )

    async def reopen_deferred_effect(self, effect_key: str) -> bool:
        updated = await self.pool.fetchval(
            """UPDATE telegram_effects SET attempts=attempts+1,last_error=NULL,updated_at=NOW()
               WHERE effect_key=$1 AND last_error LIKE $2 RETURNING effect_key""",
            effect_key,
            f"{FLOOD_PREFIX}%",
        )
        return updated is not None

    async def record_flood_wait(
        self,
        *,
        action_id: int,
        effect_key: str,
        effect_type: str,
        payload: dict[str, Any],
        telegram_seconds: int,
    ) -> int:
        await self.ensure_schema()
        wait_seconds = max(1, int(telegram_seconds)) + self.FLOOD_BUFFER_SECONDS
        peer_key = self._peer_key(payload)
        scopes = [("account", "user-session"), ("effect", effect_type)]
        if peer_key:
            scopes.append(("peer", peer_key))
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                for scope, scope_key in scopes:
                    await conn.execute(
                        """INSERT INTO traffic_cooldowns(
                             scope,scope_key,blocked_until,strikes,last_wait_seconds,updated_at)
                           VALUES($1,$2,NOW()+($3*INTERVAL '1 second'),1,$3,NOW())
                           ON CONFLICT(scope,scope_key) DO UPDATE SET
                             blocked_until=GREATEST(traffic_cooldowns.blocked_until,
                               NOW()+($3*INTERVAL '1 second')),
                             strikes=traffic_cooldowns.strikes+1,
                             last_wait_seconds=$3,updated_at=NOW()""",
                        scope,
                        scope_key,
                        wait_seconds,
                    )
                await conn.execute(
                    """INSERT INTO traffic_events(
                         event_type,effect_type,peer_key,wait_seconds,action_id)
                       VALUES('flood_wait',$1,$2,$3,$4)""",
                    effect_type,
                    peer_key,
                    wait_seconds,
                    int(action_id),
                )
                # Preserve the journal row. A rejected FloodWait invocation did not
                # mutate Telegram, so the same deterministic effect can be reopened.
                await conn.execute(
                    """UPDATE telegram_effects SET status='processing',last_error=$2,updated_at=NOW()
                       WHERE effect_key=$1""",
                    effect_key,
                    f"{FLOOD_PREFIX} {effect_type} {wait_seconds}s",
                )
                await conn.execute(
                    """UPDATE outbox_actions SET status='pending',
                         available_at=GREATEST(available_at,NOW()+($2*INTERVAL '1 second')),
                         lease_until=NULL,last_error=$3,updated_at=NOW() WHERE id=$1""",
                    int(action_id),
                    wait_seconds,
                    f"FloodWait {effect_type}: {wait_seconds}s; deferido pelo governor",
                )
        return wait_seconds

    async def note_success(self, effect_type: str, payload: dict[str, Any]) -> None:
        if effect_type == "send_panel_text":
            return
        await self.ensure_schema()
        peer_key = self._peer_key(payload)
        scopes = [("effect", effect_type)]
        if peer_key:
            scopes.append(("peer", peer_key))
        for scope, scope_key in scopes:
            await self.pool.execute(
                """UPDATE traffic_cooldowns SET strikes=GREATEST(strikes-1,0),updated_at=NOW()
                   WHERE scope=$1 AND scope_key=$2 AND blocked_until<=NOW() AND strikes>0""",
                scope,
                scope_key,
            )


class SafeTelegramEffects(TelegramEffects):
    SELF_PACED_EFFECTS = {
        "send_text", "send_text_preview", "delete_messages", "forward_message",
        "send_catalogued_media", "add_contact", "send_group_reply",
        "send_group_reply_to_message", "repost_group_text",
    }

    def __init__(self, *args, governor: TrafficGovernor, peer_store: DurablePeerStore, **kwargs):
        super().__init__(*args, **kwargs)
        self.governor = governor
        self.peer_store = peer_store

    async def perform(self, effect_key, effect_type, payload, operation, reconcile=None):
        await self.governor.guard_or_defer(
            action_id=self.action_id, effect_type=effect_type, payload=payload
        )
        if (
            effect_type != "send_panel_text"
            and effect_type not in self.SELF_PACED_EFFECTS
            and self.before_user_write is not None
        ):
            await self.before_user_write()

        reopened = await self.peer_store.reopen_waiting_effect(effect_key)
        if not reopened:
            reopened = await self.governor.reopen_deferred_effect(effect_key)
        if reopened:
            decision, previous = "execute", None
        else:
            decision, previous = await self.storage.begin_effect(
                effect_key=effect_key,
                action_id=self.action_id,
                effect_type=effect_type,
                payload=payload,
            )
        if decision == "succeeded":
            if isinstance(previous, dict) and previous.get(_EFFECT_OUTCOME_KEY) == _EFFECT_REJECTED:
                raise DefinitiveExternalEffectError(
                    f"efeito {effect_key} já foi rejeitado: "
                    f"{previous.get('error_type','erro')} {previous.get('error','')}"
                )
            return previous
        if decision == "ambiguous":
            reconciled = await reconcile() if reconcile else None
            if reconciled is not None:
                await self.storage.finish_effect(effect_key, reconciled)
                return reconciled
            raise AmbiguousExternalEffect(
                f"efeito {effect_key} ficou ambíguo; revisão necessária antes de repetir"
            )

        try:
            result = await operation()
        except PeerReferenceUnavailable as exc:
            await self.peer_store.park_effect(effect_key, self.action_id, exc.peer_id)
            raise
        except errors.FloodWaitError as exc:
            wait_seconds = await self.governor.record_flood_wait(
                action_id=self.action_id,
                effect_key=effect_key,
                effect_type=effect_type,
                payload=payload,
                telegram_seconds=int(exc.seconds),
            )
            raise DeferredTraffic(wait_seconds, f"FloodWait {effect_type}: {wait_seconds}s")
        except DefinitiveExternalEffectError as exc:
            await self.storage.finish_effect(effect_key, _rejection_result(exc))
            raise
        except DEFINITIVE_RPC_ERRORS as exc:
            await self.storage.finish_effect(effect_key, _rejection_result(exc))
            raise DefinitiveExternalEffectError(f"{type(exc).__name__}: {exc}") from exc
        except BaseException as exc:
            await self.storage.review_effect(effect_key, exc)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AmbiguousExternalEffect(
                f"resultado externo incerto em {effect_key}: {type(exc).__name__}"
            ) from exc
        await self.storage.finish_effect(effect_key, result)
        await self.governor.note_success(effect_type, payload)
        return result


class SafeOutboxWriter(OutboxWriter):
    """The single Writer, with durable FloodWait and peer deferral."""

    def __init__(self, *args, peer_store: DurablePeerStore, **kwargs):
        super().__init__(*args, **kwargs)
        self.peer_store = peer_store
        self.governor = TrafficGovernor(self.storage.pool)

    async def run(self) -> None:
        await self.governor.ensure_schema()
        if await self._wait_or_stop(self.startup_grace_seconds):
            return
        while not self._stopped.is_set():
            remaining = await self.governor.account_remaining()
            if remaining > 0:
                if await self._wait_or_stop(min(float(remaining), 60.0)):
                    return
                continue
            action = await self.storage.claim_next_action()
            if not action:
                try:
                    await asyncio.wait_for(self._stopped.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            lane = action_lane(action)
            handler = self.handlers.get((action["module_id"], action["action_type"]))
            if action["module_id"] != "core" and not self.module_enabled(action["module_id"]):
                await self.storage.fail_action(action["id"], RuntimeError(f"módulo desligado: {action['module_id']}"))
                continue
            if handler is None:
                await self.storage.fail_action(
                    action["id"], RuntimeError(f"sem handler: {action['module_id']}.{action['action_type']}")
                )
                continue
            effects = SafeTelegramEffects(
                self.storage,
                self.client,
                action["id"],
                self.panel_client,
                before_user_write=self.user_write_pacer.wait,
                flood_wait_buffer_seconds=self.flood_wait_buffer_seconds,
                governor=self.governor,
                peer_store=self.peer_store,
            )
            deferred = False
            try:
                result = await handler(action, effects)
            except DeferredTraffic as exc:
                deferred = True
                log.warning("Traffic deferred lane=%s key=%s reason=%s", lane, action["action_key"], exc.reason)
            except PeerReferenceUnavailable as exc:
                deferred = True
                await self.peer_store.park_action(action["id"], exc.peer_id)
                log.info("Peer deferred lane=%s peer=%s", lane, exc.peer_id)
            except asyncio.CancelledError:
                raise
            except DefinitiveExternalEffectError as exc:
                await self.storage.fail_action(action["id"], exc)
            except AmbiguousExternalEffect as exc:
                await self.storage.review_action(action["id"], exc)
            except Exception as exc:
                log.exception("Outbox action failed lane=%s key=%s", lane, action["action_key"])
                await self.storage.fail_action(action["id"], exc)
            else:
                await self.storage.finish_action(action["id"], result)
            if action["module_id"] != "core" and not deferred:
                if await self._wait_or_stop(self.user_action_min_interval_seconds):
                    return
