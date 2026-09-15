"""Contact-oriented group interactions inside the existing group_reply rib."""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any

from telethon import functions, types

from .catalog import normalize_text
from .contact_ledger import ContactLedger
from .group_capture import GroupCaptureStore
from .outbox import AmbiguousExternalEffect, sent_message_id, stable_random_id


ADD_INTENT_RE = re.compile(
    r"(?:\badd\b|\badiciona(?:r)?\b|\bme\s+adiciona\b|\bsalva(?:r)?\b|"
    r"\b(?:to|tô|estou)\s+de\s+ban\b|\b(?:nao|não)\s+consigo\s+chamar\b)",
    re.I,
)
ADD_REPLY_TEXT = "já add, chama lá"


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "sim"}


def _stable_delay(key: str, minimum: int = 25, maximum: int = 35) -> int:
    lo, hi = sorted((int(minimum), int(maximum)))
    width = (hi - lo) + 1
    value = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big")
    return lo + (value % width)


def classify_add_intent(text: str) -> bool:
    normalized = normalize_text(text or "")
    return bool(ADD_INTENT_RE.search(normalized))


def mentions_me(text: str, username: str | None) -> bool:
    username = (username or "").strip().lstrip("@").lower()
    if not username:
        return False
    return f"@{username}" in (text or "").lower()


def _display_name(sender) -> str:
    return " ".join(
        item
        for item in (
            getattr(sender, "first_name", None),
            getattr(sender, "last_name", None),
        )
        if item
    )[:200]


class GroupContactFlow:
    def __init__(self, pool) -> None:
        self.pool = pool
        self.contacts = ContactLedger(pool)
        self.capture = GroupCaptureStore(pool)
        self.me = None
        self.capture_enabled = _flag("GROUP_CAPTURE_P00_ENABLED", False)
        self.add_enabled = _flag("GROUP_ADD_CONTACT_FLOW", False)
        self.protection_enabled = _flag("GROUP_ENGAGEMENT_PROTECTION", False)
        self.protection_seconds = max(
            60, int(os.getenv("GROUP_ENGAGEMENT_PROTECTION_SECONDS", str(24 * 3600)))
        )

    async def on_connect(self, me) -> None:
        self.me = me
        await self.contacts.on_connect(me)
        await self.capture.ensure_schema()
        await self.pool.execute(
            """CREATE TABLE IF NOT EXISTS group_protected_messages (
                   chat_id BIGINT NOT NULL,
                   message_id BIGINT NOT NULL,
                   protected_until TIMESTAMPTZ NOT NULL,
                   protection_version INTEGER NOT NULL DEFAULT 1,
                   last_interaction_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                   reason TEXT NOT NULL,
                   created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                   updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                   PRIMARY KEY(chat_id,message_id))"""
        )
        await self.pool.execute(
            """CREATE INDEX IF NOT EXISTS group_protected_until_idx
               ON group_protected_messages(protected_until)"""
        )

    def register_actions(self, writer, module_id: str) -> None:
        writer.register(module_id, "group_add_contact_reply", self.action_add_contact_reply)
        writer.register(
            module_id,
            "cleanup_protected_group_message",
            self.action_cleanup_protected_message,
        )
        writer.register(
            module_id,
            "group_capture_contact_reply",
            self.action_capture_contact_reply,
        )
        writer.register(module_id, "group_capture_cleanup", self.action_capture_cleanup)

    async def _reply_context(self, event) -> tuple[bool, int | None]:
        if not getattr(event, "is_reply", False):
            return False, None
        try:
            replied = await event.get_reply_message()
        except Exception:
            return False, None
        if replied is None:
            return False, None
        sender_id = int(getattr(replied, "sender_id", 0) or 0)
        me_id = int(getattr(self.me, "id", 0) or 0)
        ours = bool(getattr(replied, "out", False) or (me_id and sender_id == me_id))
        return ours, int(getattr(replied, "id", 0) or 0) or None

    async def _current_repost_message(self, chat_id: int) -> int | None:
        value = await self.pool.fetchval(
            "SELECT current_message_id FROM group_repost_state WHERE chat_id=$1",
            chat_id,
        )
        return int(value) if value is not None else None

    async def _is_repost_message(self, chat_id: int, message_id: int) -> bool:
        current = await self._current_repost_message(chat_id)
        if current == message_id:
            return True
        return bool(
            await self.pool.fetchval(
                """SELECT EXISTS(
                     SELECT 1 FROM group_protected_messages
                     WHERE chat_id=$1 AND message_id=$2
                   )""",
                chat_id,
                message_id,
            )
        )

    async def _protect_message(
        self,
        *,
        chat_id: int,
        message_id: int,
        reason: str,
        event_id: int,
        module_id: str,
    ) -> None:
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                version = await conn.fetchval(
                    """INSERT INTO group_protected_messages(
                           chat_id,message_id,protected_until,protection_version,
                           last_interaction_at,reason,created_at,updated_at
                       ) VALUES(
                           $1,$2,NOW()+($3 * INTERVAL '1 second'),1,NOW(),$4,NOW(),NOW()
                       )
                       ON CONFLICT(chat_id,message_id) DO UPDATE SET
                           protected_until=NOW()+($3 * INTERVAL '1 second'),
                           protection_version=group_protected_messages.protection_version+1,
                           last_interaction_at=NOW(),reason=$4,updated_at=NOW()
                       RETURNING protection_version""",
                    chat_id,
                    message_id,
                    self.protection_seconds,
                    reason,
                )
                action_key = (
                    f"group-protected-cleanup:{chat_id}:{message_id}:v{int(version)}:{event_id}"
                )
                await conn.execute(
                    """INSERT INTO outbox_actions(
                           action_key,module_id,action_type,payload,available_at
                       ) VALUES($1,$2,'cleanup_protected_group_message',$3::jsonb,
                                NOW()+($4 * INTERVAL '1 second'))
                       ON CONFLICT(action_key) DO NOTHING""",
                    action_key,
                    module_id,
                    json.dumps(
                        {
                            "peer": chat_id,
                            "message_id": message_id,
                            "protection_version": int(version),
                        }
                    ),
                    self.protection_seconds,
                )

    async def observe_event(self, event, sender, *, module_id: str) -> bool:
        """Observe one human group event; return True when P00 captured it.

        P00 is deliberately evaluated before the older ADD/protection paths.
        When P00 mode is enabled those older contact reactions are fully dormant:
        the bait and its capture are one product, not a third reactive product.
        """
        if not (self.capture_enabled or self.add_enabled or self.protection_enabled):
            return False
        chat_id = int(event.chat_id)
        text = event.raw_text or ""
        reply_to_ours, replied_message_id = await self._reply_context(event)
        mentioned = mentions_me(text, getattr(self.me, "username", None))
        current_bait_id = (
            await self._current_repost_message(chat_id)
            if self.capture_enabled
            else None
        )

        direct_bait_reply = bool(
            current_bait_id is not None
            and reply_to_ours
            and replied_message_id == current_bait_id
        )
        capture_candidate = bool(
            self.capture_enabled
            and current_bait_id is not None
            and (direct_bait_reply or mentioned)
        )
        if capture_candidate:
            user_id = int(sender.id)
            display_name = _display_name(sender)
            await self.contacts.observe(
                user_id=user_id,
                username=getattr(sender, "username", None),
                display_name=display_name,
                source_type="group_capture",
                source_chat_id=chat_id,
                source_message_id=int(event.id),
                private_contact=False,
            )
            if bool(getattr(sender, "contact", False)):
                await self.contacts.mark_already_saved(user_id)
            await self.capture.queue_capture(
                module_id=module_id,
                chat_id=chat_id,
                bait_message_id=int(current_bait_id),
                source_message_id=int(event.id),
                user_id=user_id,
                username=getattr(sender, "username", None),
                display_name=display_name,
                first_name=getattr(sender, "first_name", None),
                last_name=getattr(sender, "last_name", None),
            )
            return True

        # P00 mode replaces, rather than stacks on top of, the old ADD and
        # engagement-protection reactions. No active bait/capture means silence.
        if self.capture_enabled:
            return False

        engaged = reply_to_ours or mentioned
        if not engaged:
            return False

        if self.protection_enabled:
            target_message_id = None
            reason = None
            if reply_to_ours and replied_message_id is not None:
                if await self._is_repost_message(chat_id, replied_message_id):
                    target_message_id = replied_message_id
                    reason = "reply"
            if target_message_id is None and mentioned:
                target_message_id = await self._current_repost_message(chat_id)
                if target_message_id is not None:
                    reason = "mention"
            if target_message_id is not None and reason is not None:
                await self._protect_message(
                    chat_id=chat_id,
                    message_id=target_message_id,
                    reason=reason,
                    event_id=int(event.id),
                    module_id=module_id,
                )

        if not self.add_enabled or not classify_add_intent(text):
            return False

        user_id = int(sender.id)
        display_name = _display_name(sender)
        await self.contacts.observe(
            user_id=user_id,
            username=getattr(sender, "username", None),
            display_name=display_name,
            source_type="group_add_request",
            source_chat_id=chat_id,
            source_message_id=int(event.id),
            private_contact=False,
        )
        if bool(getattr(sender, "contact", False)):
            await self.contacts.mark_already_saved(user_id)

        account_id = int(getattr(self.me, "id", 0) or 0)
        delay = _stable_delay(f"group-add:{chat_id}:{event.id}:{user_id}")
        await self.pool.execute(
            """INSERT INTO outbox_actions(
                   action_key,module_id,action_type,payload,available_at
               ) VALUES($1,$2,'group_add_contact_reply',$3::jsonb,
                        NOW()+($4 * INTERVAL '1 second'))
               ON CONFLICT(action_key) DO NOTHING""",
            f"group-add:{account_id}:{chat_id}:{event.id}:{user_id}",
            module_id,
            json.dumps(
                {
                    "peer": chat_id,
                    "source_message_id": int(event.id),
                    "source_user_id": user_id,
                    "account_user_id": account_id,
                    "username": getattr(sender, "username", None),
                    "display_name": display_name,
                    "first_name": getattr(sender, "first_name", None),
                    "last_name": getattr(sender, "last_name", None),
                    "text": ADD_REPLY_TEXT,
                },
                ensure_ascii=False,
            ),
            delay,
        )
        return False

    async def is_message_protected(self, chat_id: int, message_id: int) -> bool:
        return bool(
            await self.pool.fetchval(
                """SELECT EXISTS(
                     SELECT 1 FROM group_protected_messages
                     WHERE chat_id=$1 AND message_id=$2 AND protected_until > NOW()
                   )""",
                chat_id,
                message_id,
            )
        )

    async def clear_protection(self, chat_id: int, message_id: int) -> None:
        await self.pool.execute(
            "DELETE FROM group_protected_messages WHERE chat_id=$1 AND message_id=$2",
            chat_id,
            message_id,
        )

    async def action_add_contact_reply(self, action: dict[str, Any], effects) -> dict[str, Any]:
        payload = action["payload"]
        user_id = int(payload["source_user_id"])
        contact_action = {
            "action_key": action["action_key"],
            "payload": {
                "peer": user_id,
                "account_user_id": int(payload["account_user_id"]),
                "username": payload.get("username"),
                "display_name": payload.get("display_name"),
                "first_name": payload.get("first_name"),
                "last_name": payload.get("last_name"),
            },
        }
        contact = await self.contacts.save_with_effects(contact_action, effects)
        if not contact.get("saved"):
            return {"sent": False, "reason": "contact_not_saved"}

        chat_id = int(payload["peer"])
        source_message_id = int(payload["source_message_id"])
        text = str(payload.get("text") or ADD_REPLY_TEXT)
        effect_key = f"{action['action_key']}:reply"
        random_id = stable_random_id(effect_key)

        async def send_reply():
            input_peer = await effects.client.get_input_entity(chat_id)
            if effects.before_user_write is not None:
                await effects.before_user_write()
            result = await effects.client(
                functions.messages.SendMessageRequest(
                    peer=input_peer,
                    message=text,
                    random_id=random_id,
                    no_webpage=True,
                    reply_to=types.InputReplyToMessage(reply_to_msg_id=source_message_id),
                )
            )
            return {"message_id": sent_message_id(result), "random_id": random_id}

        reply = await effects.perform(
            effect_key,
            "send_group_reply_to_message",
            {
                "peer": str(chat_id),
                "reply_to_message_id": source_message_id,
                "text": text,
                "random_id": random_id,
            },
            send_reply,
        )
        return {"sent": True, "contact": contact, "reply": reply}

    async def action_capture_contact_reply(
        self, action: dict[str, Any], effects
    ) -> dict[str, Any]:
        """Save first, then reply exactly to the fish; never claim save on failure."""
        if not self.capture_enabled:
            return {"sent": False, "reason": "feature_disabled"}
        payload = dict(action["payload"])
        chat_id = int(payload["peer"])
        bait_message_id = int(payload["bait_message_id"])
        user_id = int(payload["source_user_id"])
        username = str(payload.get("username") or "").strip().lstrip("@")
        text = f"@{username} {ADD_REPLY_TEXT}" if username else ADD_REPLY_TEXT
        payload["account_user_id"] = int(getattr(self.me, "id", 0) or 0)
        payload["text"] = text
        delegated = {**action, "payload": payload}
        try:
            result = await self.action_add_contact_reply(delegated, effects)
        except AmbiguousExternalEffect as exc:
            await self.capture.mark_error(
                chat_id=chat_id,
                bait_message_id=bait_message_id,
                user_id=user_id,
                status="review",
                error=exc,
            )
            raise
        except Exception as exc:
            await self.capture.mark_error(
                chat_id=chat_id,
                bait_message_id=bait_message_id,
                user_id=user_id,
                status="failed",
                error=exc,
            )
            raise

        reply = result.get("reply") or {}
        reply_message_id = reply.get("message_id")
        if not result.get("sent") or reply_message_id is None:
            exc = RuntimeError("Telegram não confirmou a resposta da captura")
            await self.capture.mark_error(
                chat_id=chat_id,
                bait_message_id=bait_message_id,
                user_id=user_id,
                status="failed",
                error=exc,
            )
            raise exc
        await self.capture.mark_sent(
            module_id="group_reply",
            chat_id=chat_id,
            bait_message_id=bait_message_id,
            user_id=user_id,
            reply_message_id=int(reply_message_id),
        )
        return {
            **result,
            "capture": True,
            "bait_message_id": bait_message_id,
            "reply_message_id": int(reply_message_id),
        }

    async def action_capture_cleanup(
        self, action: dict[str, Any], effects
    ) -> dict[str, Any]:
        """Delete only our persisted capture reply, never bait or human content."""
        payload = action["payload"]
        chat_id = int(payload["peer"])
        bait_message_id = int(payload["bait_message_id"])
        user_id = int(payload["source_user_id"])
        reply_message_id = int(payload["reply_message_id"])
        reason = str(payload.get("reason") or "cleanup")
        if not await self.capture.cleanup_allowed(
            chat_id=chat_id,
            bait_message_id=bait_message_id,
            user_id=user_id,
            reply_message_id=reply_message_id,
        ):
            return {"deleted": False, "reason": "already_cleaned_or_changed"}
        deleted = await effects.delete_messages(
            chat_id,
            [reply_message_id],
            f"{action['action_key']}:delete",
        )
        await self.capture.mark_cleaned(
            chat_id=chat_id,
            bait_message_id=bait_message_id,
            user_id=user_id,
            reason=reason,
        )
        return {"deleted": True, "capture_cleanup": True, **deleted}

    async def action_cleanup_protected_message(
        self, action: dict[str, Any], effects
    ) -> dict[str, Any]:
        payload = action["payload"]
        chat_id = int(payload["peer"])
        message_id = int(payload["message_id"])
        version = int(payload["protection_version"])
        row = await self.pool.fetchrow(
            """SELECT protection_version,protected_until
               FROM group_protected_messages
               WHERE chat_id=$1 AND message_id=$2""",
            chat_id,
            message_id,
        )
        if row is None or int(row["protection_version"]) != version:
            return {"deleted": False, "reason": "protection_changed"}
        if await self.is_message_protected(chat_id, message_id):
            return {"deleted": False, "reason": "still_protected"}
        current_id = await self._current_repost_message(chat_id)
        if current_id == message_id:
            return {"deleted": False, "reason": "still_current"}
        deleted = await effects.delete_messages(
            chat_id,
            [message_id],
            f"{action['action_key']}:delete",
        )
        await self.clear_protection(chat_id, message_id)
        return {"deleted": True, **deleted}
