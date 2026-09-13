"""Group contact/protection extension for the existing group_reply rib."""

from __future__ import annotations

from ..group_contact_flow import GroupContactFlow
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
        self.contact_flow.register_actions(writer, self.module_id)

    async def handle_event(self, event) -> bool:
        result = await super().handle_event(event)
        if not (event.is_group or event.is_channel) or event.out:
            return result
        entity = await event.get_chat()
        if not self._allowed(entity):
            return result
        sender = await event.get_sender()
        if not sender or getattr(sender, "bot", False) or getattr(sender, "deleted", False):
            return result
        if self.me is not None and int(getattr(sender, "id", 0) or 0) == int(self.me.id):
            return result
        await self.contact_flow.observe_event(event, sender, module_id=self.module_id)
        return result

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

        if await self.contact_flow.is_message_protected(chat_id, old_message_id):
            deleted = {"deleted": False, "reason": "protected_engagement"}
        else:
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
            int(new_message_id),
            version,
        )
        return {"sent": True, "new_message_id": new_message_id, **deleted}

    def preview(self) -> str:
        base = super().preview()
        add_status = "ligado" if self.contact_flow.add_enabled else "desligado"
        protection_status = (
            "ligada" if self.contact_flow.protection_enabled else "desligada"
        )
        return (
            f"{base}\n"
            f"ADD por resposta/menção: {add_status}\n"
            f"Proteção de engajamento: {protection_status} "
            f"({self.contact_flow.protection_seconds // 3600:g} h)"
        )
