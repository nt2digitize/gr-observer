"""Delayed, allowlisted replies to inbound group messages.

The module only observes chats explicitly configured in GROUP_REPLY_ALLOWLIST.
It queues one delayed response per matched inbound message, keeps a per-chat
cooldown, ignores bots/self/outgoing messages, and records the interaction so
Radar can attribute a later private message to the source group.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from telethon import errors, utils

from ..catalog import normalize_text

TRIGGERS = (
    r"\bquem\s+(?:quer|vai)\s+ver\s+(?:uma\s+)?esposa\b",
    r"\bquer(?:em)?\s+ver\s+(?:uma\s+)?esposa\b",
    r"\balgum(?:a)?\s+esposa\b",
    r"\bmostra(?:r)?\s+(?:a|uma)\s+esposa\b",
    r"\btem\s+esposa\s+(?:a[ií]|aqui)\b",
    r"\besposa\s+no\s+pv\b",
)
TRIGGER_RE = re.compile("|".join(f"(?:{item})" for item in TRIGGERS), re.I)

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


def _stable_fraction(key: str) -> float:
    raw = hashlib.sha256(key.encode("utf-8")).digest()[:8]
    return int.from_bytes(raw, "big") / ((1 << 64) - 1)


def _delay_seconds(key: str, minimum: int, maximum: int) -> int:
    lo, hi = sorted((int(minimum), int(maximum)))
    return round(lo + ((hi - lo) * _stable_fraction(key)))


def _reply_for(key: str, replies: tuple[str, ...]) -> str:
    digest = hashlib.sha256(f"reply:{key}".encode()).digest()
    return replies[int.from_bytes(digest[:4], "big") % len(replies)]


def _allow_tokens() -> tuple[str, ...]:
    return tuple(normalize_text(x).lstrip("@") for x in _csv("GROUP_REPLY_ALLOWLIST"))


class GroupReplyModule:
    module_id = "group_reply"

    def __init__(self, storage, settings):
        self.storage = storage
        self.pool = storage.pool
        self.settings = settings
        self.client = None
        self.me = None
        self.allowlist = _allow_tokens()
        custom = _csv("GROUP_REPLY_RESPONSES")
        self.replies = custom or DEFAULT_REPLIES
        self.min_delay = max(0, int(os.getenv("GROUP_REPLY_MIN_DELAY_SECONDS", "180")))
        self.max_delay = max(self.min_delay, int(os.getenv("GROUP_REPLY_MAX_DELAY_SECONDS", "420")))
        self.cooldown = max(0, int(os.getenv("GROUP_REPLY_CHAT_COOLDOWN_SECONDS", "600")))

    async def on_connect(self, client, me) -> None:
        self.client = client
        self.me = me
        await self.pool.execute(
            """CREATE TABLE IF NOT EXISTS group_reply_events (
                   chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
                   user_id BIGINT NOT NULL, matched_text TEXT, reply_text TEXT,
                   status TEXT NOT NULL DEFAULT 'queued', outbound_message_id BIGINT,
                   created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), sent_at TIMESTAMPTZ,
                   PRIMARY KEY(chat_id,message_id))"""
        )
        await self.pool.execute(
            """CREATE INDEX IF NOT EXISTS group_reply_sent_idx
               ON group_reply_events(chat_id,status,sent_at DESC)"""
        )

    async def on_disconnect(self) -> None:
        self.client = None
        self.me = None

    def register_actions(self, writer) -> None:
        writer.register(self.module_id, "send_group_reply", self.action_send_group_reply)

    def preview(self) -> str:
        groups = ", ".join(self.allowlist) if self.allowlist else "nenhum"
        return (
            "ATENDIMENTO DE GRUPOS\n"
            f"Grupos permitidos: {groups}\n"
            f"Atraso: {self.min_delay}–{self.max_delay} s\n"
            f"Cooldown por grupo: {self.cooldown} s\n"
            f"Respostas disponíveis: {len(self.replies)}\n"
            "Somente mensagens novas de pessoas; bots, mensagens próprias e duplicadas são ignorados."
        )

    def _allowed(self, entity) -> bool:
        if not self.allowlist:
            return False
        chat_id = str(utils.get_peer_id(entity))
        raw_id = str(getattr(entity, "id", ""))
        username = normalize_text(getattr(entity, "username", None) or "").lstrip("@")
        candidates = {normalize_text(chat_id), normalize_text(raw_id)}
        if username:
            candidates.add(username)
        return any(token in candidates for token in self.allowlist)

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

    async def handle_event(self, event) -> bool:
        if event.out or not (event.is_group or event.is_channel):
            return False
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False) or getattr(sender, "deleted", False):
            return False
        if self.me is not None and int(getattr(sender, "id", 0) or 0) == int(self.me.id):
            return False
        entity = await event.get_chat()
        if not self._allowed(entity):
            return False
        text = (event.raw_text or "").strip()
        if not text or not TRIGGER_RE.search(text):
            return False

        chat_id = int(utils.get_peer_id(entity))
        sender_id = int(sender.id)
        can_send, slowmode = await self._can_send(entity)
        if not can_send:
            return False

        event_key = f"{chat_id}:{event.id}"
        minimum = max(self.min_delay, slowmode)
        delay = _delay_seconds(event_key, minimum, max(minimum, self.max_delay))
        reply = _reply_for(event_key, self.replies)

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                inserted = await conn.fetchval(
                    """INSERT INTO group_reply_events(chat_id,message_id,user_id,matched_text,status,created_at)
                       VALUES($1,$2,$3,$4,'queued',NOW())
                       ON CONFLICT(chat_id,message_id) DO NOTHING RETURNING message_id""",
                    chat_id, int(event.id), sender_id, text[:500],
                )
                if inserted is None:
                    return False
                recent = await conn.fetchval(
                    """SELECT EXISTS(SELECT 1 FROM group_reply_events
                       WHERE chat_id=$1 AND status='sent'
                       AND sent_at > NOW() - ($2 * INTERVAL '1 second'))""",
                    chat_id, self.cooldown,
                )
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
        recent = await self.pool.fetchval(
            """SELECT EXISTS(SELECT 1 FROM group_reply_events
               WHERE chat_id=$1 AND status='sent'
               AND sent_at > NOW() - ($2 * INTERVAL '1 second'))""",
            chat_id, self.cooldown,
        )
        if recent:
            await self.pool.execute(
                "UPDATE group_reply_events SET status='cooldown' WHERE chat_id=$1 AND message_id=$2",
                chat_id, source_message_id,
            )
            return {"sent": False, "reason": "cooldown"}
        result = await effects.send_text(chat_id, payload["text"], f"{action['action_key']}:send")
        await self.pool.execute(
            """UPDATE group_reply_events SET status='sent',reply_text=$3,sent_at=NOW(),outbound_message_id=$4
               WHERE chat_id=$1 AND message_id=$2""",
            chat_id, source_message_id, payload["text"], result.get("message_id"),
        )
        return {"sent": True, **result}
