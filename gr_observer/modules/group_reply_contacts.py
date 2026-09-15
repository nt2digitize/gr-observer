"""Group contact/protection extension for the existing group_reply rib."""

from __future__ import annotations

import asyncio

from telethon import functions, types

from ..group_contact_flow import ADD_REPLY_TEXT, GroupContactFlow
from ..human_timing import typing_seconds
from .group_reply import GroupReplyModule


class GroupReplyWithContacts(GroupReplyModule):
    def __init__(self, storage, settings):
        super().__init__(storage, settings)
        self.contact_flow = GroupContactFlow(self.pool)

    async def on_connect(self, client, me) -> None:
        await super().on_connect(client, me)
        await self.contact_flow.on_connect(me)

    def register_actions(self, writer) -> None:
        super().register_actions(writer)
        writer.register(
            self.module_id,
            "group_add_contact_reply",
            self.action_group_add_contact_reply,
        )
        writer.register(
            self.module_id,
            "cleanup_protected_group_message",
            self.action_cleanup_protected_group_message,
        )
        writer.register(
            self.module_id,
            "group_capture_contact_reply",
            self.action_group_capture_contact_reply,
        )
        writer.register(
            self.module_id,
            "group_capture_cleanup",
            self.action_group_capture_cleanup,
        )

    async def handle_event(self, event) -> bool:
        """Let P00 reserve a bait response before the legacy reactive matcher.

        ``super`` still receives every event, therefore activity/cadence counting
        remains unchanged. A captured event has already reserved its
        ``group_reply_events`` key, so an old generic trigger cannot enqueue a
        second reply for the same human message.
        """
        if not (event.is_group or event.is_channel) or event.out:
            return await super().handle_event(event)

        entity = await event.get_chat()
        if not self._allowed(entity):
            return await super().handle_event(event)
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False) or getattr(sender, "deleted", False):
            return await super().handle_event(event)
        if self.me is not None and int(getattr(sender, "id", 0) or 0) == int(self.me.id):
            return await super().handle_event(event)

        await self.contact_flow.observe_event(event, sender, module_id=self.module_id)
        return await super().handle_event(event)

    async def _show_human_typing(self, effects, peer: int, text: str, key: str) -> None:
        """Final visible typing phase; long reading waits stay in durable scheduling."""
        seconds = typing_seconds(text, key)
        if seconds <= 0:
            return
        try:
            if effects.before_user_write is not None:
                await effects.before_user_write()
            input_peer = await effects.client.get_input_entity(peer)
            await effects.client(
                functions.messages.SetTypingRequest(
                    peer=input_peer,
                    action=types.SendMessageTypingAction(),
                )
            )
        except Exception:
            pass
        await asyncio.sleep(seconds)

    async def _current_managed_message(self, chat_id: int) -> tuple[int | None, int | None]:
        """Return the current visible slot and template version when a loop exists."""
        row = await self.pool.fetchrow(
            "SELECT current_message_id,template_version FROM group_repost_state WHERE chat_id=$1",
            chat_id,
        )
        if row is not None:
            return int(row["current_message_id"]), int(row["template_version"])
        previous = await self.pool.fetchval(
            """SELECT outbound_message_id FROM group_reply_events
               WHERE chat_id=$1 AND status='sent' AND outbound_message_id IS NOT NULL
               ORDER BY sent_at DESC LIMIT 1""",
            chat_id,
        )
        return (None if previous is None else int(previous)), None

    async def action_send_group_reply(self, action: dict, effects) -> dict:
        payload = action["payload"]
        chat_id = int(payload["peer"])
        source_message_id = int(payload["source_message_id"])
        row = await self.pool.fetchrow(
            "SELECT status FROM group_reply_events WHERE chat_id=$1 AND message_id=$2",
            chat_id,
            source_message_id,
        )
        if not row or row["status"] != "queued":
            return {"sent": False, "reason": "state_changed"}
        recent = await self._in_cooldown(self.pool, chat_id)
        if recent:
            await self.pool.execute(
                "UPDATE group_reply_events SET status='cooldown' WHERE chat_id=$1 AND message_id=$2",
                chat_id,
                source_message_id,
            )
            return {"sent": False, "reason": "cooldown"}

        old_message_id, expected_version = await self._current_managed_message(chat_id)
        text = str(payload.get("text") or "")
        if text:
            await self._show_human_typing(
                effects,
                chat_id,
                text,
                f"{action['action_key']}:typing",
            )
        self._mark_automated_outbound(chat_id, text)
        result = await effects.send_text(
            chat_id,
            text,
            f"{action['action_key']}:send",
        )
        new_message_id = result.get("message_id")
        if new_message_id is None:
            raise RuntimeError("Telegram não confirmou o ID da resposta do grupo")
        new_message_id = int(new_message_id)

        if expected_version is not None:
            current = await self.pool.fetchrow(
                "SELECT current_message_id,template_version FROM group_repost_state WHERE chat_id=$1",
                chat_id,
            )
            if (
                current is None
                or int(current["template_version"]) != expected_version
                or int(current["current_message_id"]) != old_message_id
            ):
                await effects.delete_messages(
                    chat_id,
                    [new_message_id],
                    f"{action['action_key']}:delete-stale",
                )
                await self.pool.execute(
                    """UPDATE group_reply_events SET status='superseded',reply_text=$3,
                       outbound_message_id=$4 WHERE chat_id=$1 AND message_id=$2""",
                    chat_id,
                    source_message_id,
                    text,
                    new_message_id,
                )
                return {"sent": False, "reason": "newer_group_message"}

        if old_message_id is not None and old_message_id != new_message_id:
            deleted = await effects.delete_messages(
                chat_id,
                [old_message_id],
                f"{action['action_key']}:delete-old",
            )
            await self.contact_flow.clear_protection(chat_id, old_message_id)
        else:
            deleted = {"deleted": False, "reason": "no_previous_managed_message"}

        if expected_version is not None:
            await self.pool.execute(
                """UPDATE group_repost_state SET current_message_id=$2,
                   last_post_at=NOW(),updated_at=NOW()
                   WHERE chat_id=$1 AND template_version=$3""",
                chat_id,
                new_message_id,
                expected_version,
            )
        await self.pool.execute(
            """UPDATE group_reply_events SET status='sent',reply_text=$3,sent_at=NOW(),
               outbound_message_id=$4 WHERE chat_id=$1 AND message_id=$2""",
            chat_id,
            source_message_id,
            text,
            new_message_id,
        )
        return {"sent": True, **result, **deleted}

    async def action_group_add_contact_reply(self, action: dict, effects) -> dict:
        if not self.contact_flow.add_enabled:
            return {"sent": False, "reason": "feature_disabled"}
        payload = action["payload"]
        text = str(payload.get("text") or "")
        if text:
            await self._show_human_typing(
                effects,
                int(payload["peer"]),
                text,
                f"{action['action_key']}:typing",
            )
        return await self.contact_flow.action_add_contact_reply(action, effects)

    async def action_group_capture_contact_reply(self, action: dict, effects) -> dict:
        if not self.contact_flow.capture_enabled:
            return {"sent": False, "reason": "feature_disabled"}
        payload = action["payload"]
        username = str(payload.get("username") or "").strip().lstrip("@")
        text = f"@{username} {ADD_REPLY_TEXT}" if username else ADD_REPLY_TEXT
        await self._show_human_typing(
            effects,
            int(payload["peer"]),
            text,
            f"{action['action_key']}:typing",
        )
        return await self.contact_flow.action_capture_contact_reply(action, effects)

    async def action_group_capture_cleanup(self, action: dict, effects) -> dict:
        return await self.contact_flow.action_capture_cleanup(action, effects)

    async def action_cleanup_protected_group_message(self, action: dict, effects) -> dict:
        return await self.contact_flow.action_cleanup_protected_message(action, effects)

    async def action_repost_group_text(self, action: dict, effects) -> dict:
        payload = action["payload"]
        chat_id = int(payload["peer"])
        version = int(payload["template_version"])
        state = await self.pool.fetchrow(
            """SELECT template_text,current_message_id,template_version,repost_pending,
                      repost_approved_at
               FROM group_repost_state WHERE chat_id=$1""",
            chat_id,
        )
        if (
            not state
            or not state["repost_pending"]
            or int(state["template_version"]) != version
        ):
            return {"sent": False, "reason": "template_changed"}

        if (
            await self._reply_blocks_repost(chat_id)
            and not await self._repost_max_defer_elapsed(chat_id)
        ):
            return await self._defer_repost_action(action, chat_id)

        text = str(state["template_text"])
        old_message_id = int(state["current_message_id"])
        self._mark_automated_outbound(chat_id, text)
        await self._show_human_typing(
            effects,
            chat_id,
            text,
            f"{action['action_key']}:typing",
        )
        sent = await effects.send_text(
            chat_id,
            text,
            f"{action['action_key']}:send",
        )
        new_message_id = sent.get("message_id")
        if new_message_id is None:
            raise RuntimeError("Telegram não confirmou o ID da nova publicação")
        new_message_id = int(new_message_id)
        current_version = await self.pool.fetchval(
            "SELECT template_version FROM group_repost_state WHERE chat_id=$1",
            chat_id,
        )
        if current_version is None or int(current_version) != version:
            await effects.delete_messages(
                chat_id,
                [new_message_id],
                f"{action['action_key']}:delete-stale",
            )
            return {"sent": False, "reason": "template_changed_during_send"}

        deleted = await effects.delete_messages(
            chat_id,
            [old_message_id],
            f"{action['action_key']}:delete-old",
        )
        await self.contact_flow.clear_protection(chat_id, old_message_id)

        await self.pool.execute(
            """UPDATE group_repost_state SET current_message_id=$2,
               repost_pending=FALSE,last_post_at=NOW(),repost_approved_at=NULL,
               cycle_target_messages=NULL,cycle_min_interval_seconds=NULL,
               cycle_planned_at=NULL,cycle_valid_until=NULL,
               cycle_activity_score=NULL,updated_at=NOW()
               WHERE chat_id=$1 AND template_version=$3""",
            chat_id,
            new_message_id,
            version,
        )
        return {"sent": True, "new_message_id": new_message_id, **deleted}

    def preview(self) -> str:
        base = super().preview()
        capture_status = "ligada" if self.contact_flow.capture_enabled else "desligada"
        add_status = "ligado" if self.contact_flow.add_enabled else "desligado"
        protection_status = (
            "ligada" if self.contact_flow.protection_enabled else "desligada"
        )
        return (
            f"{base}\n"
            f"Captura P00 da isca ativa: {capture_status}\n"
            "Gatilho P00: reply direto à isca ativa ou @menção enquanto ela estiver ativa.\n"
            "A captura salva antes de confirmar, responde em reply e não altera a vida da isca.\n"
            "Resposta de captura: limpa ao chegar no PV ou em até 45 min.\n"
            f"ADD legado por resposta/menção: {add_status}\n"
            f"Proteção legada de engajamento: {protection_status} "
            f"({self.contact_flow.protection_seconds // 3600:g} h; nunca aplicada à captura P00)\n"
            "Retenção visual da isca: no máximo uma mensagem gerenciada por grupo.\n"
            "Respostas automáticas usam digitação proporcional; os atrasos de leitura continuam duráveis."
        )
