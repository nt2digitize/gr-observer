"""Delayed, allowlisted replies to inbound group messages.

The module observes chats explicitly configured in GROUP_REPLY_ALLOWLIST and
chats authorized by a successful manual text post from the user account.
Allowlist items may be Telegram chat IDs, @usernames, or private invite links.
Private invite links are resolved at connect time when the user account is
already a member. The module queues one delayed response per matched inbound
message, keeps a per-chat cooldown, ignores bots/self/outgoing messages, and
records the interaction so Radar can attribute a later private message to the
source group. A manual text becomes that chat's repost template; after ten new
human messages, a confirmed replacement is sent before the previous copy is
deleted.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re

from telethon import errors, utils
from telethon.tl.functions.messages import CheckChatInviteRequest

from ..catalog import normalize_text

log = logging.getLogger("gr-observer.group-reply")

TRIGGERS = (
    r"\bquem\s+(?:quer|vai)\s+ver\s+(?:uma\s+)?esposa\b",
    r"\bquer(?:em)?\s+ver\s+(?:uma\s+)?esposa\b",
    r"\balgum(?:a)?\s+esposa\b",
    r"\bmostra(?:r)?\s+(?:a|uma)\s+esposa\b",
    r"\btem\s+esposa\s+(?:a[ií]|aqui)\b",
    r"\besposa\s+no\s+pv\b",
)
TRIGGER_RE = re.compile("|".join(f"(?:{item})" for item in TRIGGERS), re.I)
INVITE_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)([A-Za-z0-9_-]+)(?:\?.*)?$",
    re.I,
)

DEFAULT_REPLIES = (
    "chama no pv 😉", "tenho sim, chama no pv", "quer ver? chama no pv 😉",
    "aqui tem 😏 chama no pv", "chama no privado q te mostro", "tem esposa sim 😉 pv",
    "quer conhecer? chama no pv", "pv aberto 😉", "chama aí no pv",
    "tenho uma pra te mostrar 😏", "vem no pv 😉", "quer ver a minha? chama no pv",
    "chama no privado 😉", "no pv eu te mostro", "a minha aparece no pv 😏",
    "tem sim, chama aí", "quer uma esposa? pv 😉", "chega no pv",
    "manda um oi no pv 😉", "chama q eu te mostro 😏",
)


def _csv(name: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in os.getenv(name, "").split(",") if x.strip())


def invite_hash(value: str) -> str | None:
    match = INVITE_RE.match((value or "").strip())
    return match.group(1) if match else None


def _stable_fraction(key: str) -> float:
    raw = hashlib.sha256(key.encode("utf-8")).digest()[:8]
    return int.from_bytes(raw, "big") / ((1 << 64) - 1)


def _delay_seconds(key: str, minimum: int, maximum: int) -> int:
    lo, hi = sorted((int(minimum), int(maximum)))
    return round(lo + ((hi - lo) * _stable_fraction(key)))


def _reply_for(key: str, replies: tuple[str, ...]) -> str:
    digest = hashlib.sha256(f"reply:{key}".encode()).digest()
    return replies[int.from_bytes(digest[:4], "big") % len(replies)]


def _normalized_direct_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        normalize_text(value).lstrip("@")
        for value in values
        if not invite_hash(value)
    )


class GroupReplyModule:
    module_id = "group_reply"

    def __init__(self, storage, settings):
        self.storage = storage
        self.pool = storage.pool
        self.settings = settings
        self.client = None
        self.me = None
        self.allowlist_raw = _csv("GROUP_REPLY_ALLOWLIST")
        self.allowlist = _normalized_direct_tokens(self.allowlist_raw)
        self.resolved_chat_ids: set[int] = set()
        self.dynamic_chat_ids: set[int] = set()
        self.unresolved_invites: list[str] = []
        self._automated_outbound: dict[tuple[int, str], float] = {}
        custom = _csv("GROUP_REPLY_RESPONSES")
        self.replies = custom or DEFAULT_REPLIES
        self.min_delay = max(0, int(os.getenv("GROUP_REPLY_MIN_DELAY_SECONDS", "70")))
        self.max_delay = max(self.min_delay, int(os.getenv("GROUP_REPLY_MAX_DELAY_SECONDS", "90")))
        self.min_cooldown = max(
            0, int(os.getenv("GROUP_REPLY_MIN_COOLDOWN_SECONDS", "95"))
        )
        self.max_cooldown = max(
            self.min_cooldown,
            int(os.getenv("GROUP_REPLY_MAX_COOLDOWN_SECONDS", "110")),
        )

    async def on_connect(self, client, me) -> None:
        self.client = client
        self.me = me
        await self.pool.execute(
            """CREATE TABLE IF NOT EXISTS group_reply_events (
                   chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
                   user_id BIGINT NOT NULL, matched_text TEXT, reply_text TEXT,
                   status TEXT NOT NULL DEFAULT 'queued', outbound_message_id BIGINT,
                   cooldown_seconds INTEGER,
                   created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), sent_at TIMESTAMPTZ,
                   PRIMARY KEY(chat_id,message_id))"""
        )
        await self.pool.execute(
            "ALTER TABLE group_reply_events ADD COLUMN IF NOT EXISTS cooldown_seconds INTEGER"
        )
        await self.pool.execute(
            """CREATE TABLE IF NOT EXISTS group_repost_state (
                   chat_id BIGINT PRIMARY KEY,
                   template_text TEXT NOT NULL,
                   current_message_id BIGINT NOT NULL,
                   inbound_count INTEGER NOT NULL DEFAULT 0,
                   template_version INTEGER NOT NULL DEFAULT 1,
                   repost_pending BOOLEAN NOT NULL DEFAULT FALSE,
                   updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"""
        )
        await self.pool.execute(
            """CREATE INDEX IF NOT EXISTS group_reply_sent_idx
               ON group_reply_events(chat_id,status,sent_at DESC)"""
        )
        rows = await self.pool.fetch("SELECT chat_id FROM group_repost_state")
        self.dynamic_chat_ids = {int(row["chat_id"]) for row in rows}
        await self._resolve_invite_allowlist()

    async def on_disconnect(self) -> None:
        self.client = None
        self.me = None
        self.resolved_chat_ids.clear()
        self.dynamic_chat_ids.clear()
        self.unresolved_invites.clear()
        self._automated_outbound.clear()

    async def _resolve_invite_allowlist(self) -> None:
        self.resolved_chat_ids.clear()
        self.unresolved_invites.clear()
        for value in self.allowlist_raw:
            token = invite_hash(value)
            if not token:
                continue
            try:
                result = await self.client(CheckChatInviteRequest(token))
                chat = getattr(result, "chat", None)
                if chat is None:
                    self.unresolved_invites.append(value)
                    continue
                self.resolved_chat_ids.add(int(utils.get_peer_id(chat)))
            except errors.FloodWaitError:
                raise
            except Exception as exc:
                self.unresolved_invites.append(value)
                log.warning("Falha ao resolver invite da allowlist: %s", type(exc).__name__)
        log.info(
            "Atendimento de Grupos: %s invite(s) resolvido(s), %s pendente(s)",
            len(self.resolved_chat_ids), len(self.unresolved_invites),
        )

    def register_actions(self, writer) -> None:
        writer.register(self.module_id, "send_group_reply", self.action_send_group_reply)
        writer.register(self.module_id, "repost_group_text", self.action_repost_group_text)

    def preview(self) -> str:
        invite_count = sum(1 for value in self.allowlist_raw if invite_hash(value))
        return (
            "ATENDIMENTO DE GRUPOS\n"
            f"Alvos configurados: {len(self.allowlist_raw)} "
            f"({invite_count} link(s), {len(self.allowlist)} ID/@username)\n"
            f"Links resolvidos nesta conexão: {len(self.resolved_chat_ids)}\n"
            f"Links pendentes: {len(self.unresolved_invites)}\n"
            f"Grupos autorizados por postagem manual: {len(self.dynamic_chat_ids)}\n"
            f"Atraso: {self.min_delay}–{self.max_delay} s\n"
            f"Cooldown por grupo: {self.min_cooldown}–{self.max_cooldown} s\n"
            "Republicação: após 10 mensagens novas de outras pessoas\n"
            f"Respostas disponíveis: {len(self.replies)}\n"
            "Somente mensagens novas de pessoas; bots, mensagens próprias e duplicadas são ignorados."
        )

    def _allowed(self, entity) -> bool:
        chat_id_int = int(utils.get_peer_id(entity))
        if chat_id_int in self.resolved_chat_ids:
            return True
        if chat_id_int in self.dynamic_chat_ids:
            return True
        if not self.allowlist:
            return False
        raw_id = str(getattr(entity, "id", ""))
        username = normalize_text(getattr(entity, "username", None) or "").lstrip("@")
        candidates = {normalize_text(str(chat_id_int)), normalize_text(raw_id)}
        if username:
            candidates.add(username)
        return any(token in candidates for token in self.allowlist)

    def _mark_automated_outbound(self, chat_id: int, text: str) -> None:
        self._automated_outbound[(chat_id, text)] = (
            asyncio.get_running_loop().time() + 30
        )

    def _is_automated_outbound(self, chat_id: int, text: str) -> bool:
        current = asyncio.get_running_loop().time()
        self._automated_outbound = {
            key: expiry
            for key, expiry in self._automated_outbound.items()
            if expiry > current
        }
        return self._automated_outbound.pop((chat_id, text), None) is not None

    async def _record_manual_template(self, event, entity) -> bool:
        text = (event.raw_text or "").strip()
        if not text or getattr(event, "media", None):
            return False
        chat_id = int(utils.get_peer_id(entity))
        if self._is_automated_outbound(chat_id, text):
            return True
        await self.pool.execute(
            """INSERT INTO group_repost_state(
                   chat_id,template_text,current_message_id,inbound_count,
                   template_version,repost_pending,updated_at
               ) VALUES($1,$2,$3,0,1,FALSE,NOW())
               ON CONFLICT(chat_id) DO UPDATE SET
                   template_text=EXCLUDED.template_text,
                   current_message_id=EXCLUDED.current_message_id,
                   inbound_count=0,
                   template_version=group_repost_state.template_version+1,
                   repost_pending=FALSE,updated_at=NOW()""",
            chat_id,
            text,
            int(event.id),
        )
        self.dynamic_chat_ids.add(chat_id)
        return True

    async def _count_and_queue_repost(self, chat_id: int, event_id: int) -> None:
        if chat_id not in self.dynamic_chat_ids:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                state = await conn.fetchrow(
                    """SELECT template_text,current_message_id,inbound_count,
                              template_version,repost_pending
                       FROM group_repost_state WHERE chat_id=$1 FOR UPDATE""",
                    chat_id,
                )
                if not state or state["repost_pending"]:
                    return
                count = int(state["inbound_count"]) + 1
                if count < 10:
                    await conn.execute(
                        """UPDATE group_repost_state SET inbound_count=$2,
                           updated_at=NOW() WHERE chat_id=$1""",
                        chat_id,
                        count,
                    )
                    return
                version = int(state["template_version"])
                action_key = f"group-repost:{chat_id}:{version}:{event_id}"
                await conn.execute(
                    """UPDATE group_repost_state SET inbound_count=0,
                       repost_pending=TRUE,updated_at=NOW() WHERE chat_id=$1""",
                    chat_id,
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload
                       ) VALUES($1,$2,'repost_group_text',$3::jsonb)
                       ON CONFLICT(action_key) DO NOTHING""",
                    action_key,
                    self.module_id,
                    json.dumps(
                        {
                            "peer": chat_id,
                            "text": state["template_text"],
                            "old_message_id": int(state["current_message_id"]),
                            "template_version": version,
                        },
                        ensure_ascii=False,
                    ),
                )

    async def _can_send(self, entity) -> tuple[bool, int]:
        slowmode = 0
        try:
            permissions = await self.client.get_permissions(entity, self.me)
            if getattr(permissions, "is_banned", False):
                return False, slowmode
        except errors.FloodWaitError:
            raise
        except Exception:
            pass
        row = await self.pool.fetchrow(
            "SELECT can_text, slowmode_seconds FROM chats WHERE chat_id=$1",
            utils.get_peer_id(entity),
        )
        if row:
            if row["can_text"] is False:
                return False, int(row["slowmode_seconds"] or 0)
            slowmode = int(row["slowmode_seconds"] or 0)
        return True, slowmode

    async def _in_cooldown(self, database, chat_id: int) -> bool:
        return bool(
            await database.fetchval(
                """SELECT EXISTS(
                       SELECT 1 FROM (
                           SELECT sent_at,cooldown_seconds
                           FROM group_reply_events
                           WHERE chat_id=$1 AND status='sent'
                           ORDER BY sent_at DESC LIMIT 1
                       ) latest
                       WHERE sent_at > NOW() - (
                           COALESCE(cooldown_seconds,$2) * INTERVAL '1 second'
                       )
                   )""",
                chat_id,
                self.max_cooldown,
            )
        )

    async def handle_event(self, event) -> bool:
        if not (event.is_group or event.is_channel):
            return False
        entity = await event.get_chat()
        if event.out:
            return await self._record_manual_template(event, entity)
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False) or getattr(sender, "deleted", False):
            return False
        if self.me is not None and int(getattr(sender, "id", 0) or 0) == int(self.me.id):
            return False
        chat_id = int(utils.get_peer_id(entity))
        await self._count_and_queue_repost(chat_id, int(event.id))
        if not self._allowed(entity):
            return False
        text = (event.raw_text or "").strip()
        if not text or not TRIGGER_RE.search(text):
            return False

        sender_id = int(sender.id)
        can_send, slowmode = await self._can_send(entity)
        if not can_send:
            return False

        event_key = f"{chat_id}:{event.id}"
        minimum = max(self.min_delay, slowmode)
        delay = _delay_seconds(event_key, minimum, max(minimum, self.max_delay))
        cooldown = _delay_seconds(
            f"cooldown:{event_key}", self.min_cooldown, self.max_cooldown
        )
        reply = _reply_for(event_key, self.replies)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO group_reply_events(
                           chat_id,message_id,user_id,matched_text,status,cooldown_seconds,created_at
                       ) VALUES($1,$2,$3,$4,'queued',$5,NOW())
                       ON CONFLICT(chat_id,message_id) DO NOTHING RETURNING message_id""",
                    chat_id, int(event.id), sender_id, text[:500], cooldown,
                )
                if inserted is None:
                    return False
                recent = await self._in_cooldown(conn, chat_id)
                if recent:
                    await conn.execute(
                        "UPDATE group_reply_events SET status='cooldown' WHERE chat_id=$1 AND message_id=$2",
                        chat_id, int(event.id),
                    )
                    return False
                await conn.execute(
                    """INSERT INTO inbox_events(source,event_key,module_id,payload)
                       VALUES('telegram-user',$1,$2,$3::jsonb) ON CONFLICT DO NOTHING""",
                    event_key, self.module_id, "{}",
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(action_key,module_id,action_type,payload,available_at)
                       VALUES($1,$2,'send_group_reply',$3::jsonb,NOW()+($4 * INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    f"group-reply:{event_key}", self.module_id,
                    json.dumps({"peer": chat_id, "source_message_id": int(event.id),
                                "source_user_id": sender_id, "text": reply}, ensure_ascii=False),
                    delay,
                )
                await conn.execute(
                    """INSERT INTO group_interactions(user_id,chat_id,message_id,interaction_type,observed_at)
                       VALUES($1,$2,$3,'group_reply_trigger',NOW()) ON CONFLICT DO NOTHING""",
                    sender_id, chat_id, int(event.id),
                )
        return False

    async def action_send_group_reply(self, action: dict, effects) -> dict:
        payload = action["payload"]
        chat_id = int(payload["peer"])
        source_message_id = int(payload["source_message_id"])
        row = await self.pool.fetchrow(
            "SELECT status FROM group_reply_events WHERE chat_id=$1 AND message_id=$2",
            chat_id, source_message_id,
        )
        if not row or row["status"] != "queued":
            return {"sent": False, "reason": "state_changed"}
        recent = await self._in_cooldown(self.pool, chat_id)
        if recent:
            await self.pool.execute(
                "UPDATE group_reply_events SET status='cooldown' WHERE chat_id=$1 AND message_id=$2",
                chat_id, source_message_id,
            )
            return {"sent": False, "reason": "cooldown"}
        self._mark_automated_outbound(chat_id, payload["text"])
        result = await effects.send_text(
            chat_id, payload["text"], f"{action['action_key']}:send"
        )
        await self.pool.execute(
            """UPDATE group_reply_events SET status='sent',reply_text=$3,sent_at=NOW(),outbound_message_id=$4
               WHERE chat_id=$1 AND message_id=$2""",
            chat_id, source_message_id, payload["text"], result.get("message_id"),
        )
        return {"sent": True, **result}

    async def action_repost_group_text(self, action: dict, effects) -> dict:
        payload = action["payload"]
        chat_id = int(payload["peer"])
        version = int(payload["template_version"])
        state = await self.pool.fetchrow(
            """SELECT template_text,current_message_id,template_version,repost_pending
               FROM group_repost_state WHERE chat_id=$1""",
            chat_id,
        )
        if (
            not state
            or not state["repost_pending"]
            or int(state["template_version"]) != version
        ):
            return {"sent": False, "reason": "template_changed"}
        text = str(state["template_text"])
        old_message_id = int(state["current_message_id"])
        self._mark_automated_outbound(chat_id, text)
        sent = await effects.send_text(
            chat_id, text, f"{action['action_key']}:send"
        )
        new_message_id = sent.get("message_id")
        if new_message_id is None:
            raise RuntimeError("Telegram não confirmou o ID da nova publicação")
        current_version = await self.pool.fetchval(
            "SELECT template_version FROM group_repost_state WHERE chat_id=$1",
            chat_id,
        )
        if current_version is None or int(current_version) != version:
            await effects.delete_messages(
                chat_id,
                [int(new_message_id)],
                f"{action['action_key']}:delete-stale",
            )
            return {"sent": False, "reason": "template_changed_during_send"}
        deleted = await effects.delete_messages(
            chat_id,
            [old_message_id],
            f"{action['action_key']}:delete-old",
        )
        await self.pool.execute(
            """UPDATE group_repost_state SET current_message_id=$2,
               repost_pending=FALSE,updated_at=NOW()
               WHERE chat_id=$1 AND template_version=$3""",
            chat_id,
            int(new_message_id),
            version,
        )
        return {"sent": True, "new_message_id": new_message_id, **deleted}
