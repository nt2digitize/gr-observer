"""PV contact persistence layered on the existing pv_reply rib."""

from __future__ import annotations

import os

from ..contact_ledger import ContactLedger
from ..pv_message_runtime import PvMessageRuntimeMixin
from ..pv_message_steps import POSITION_GAP, PvMessageStepStore
from .pv_reply import PvReplyModule, display_name, is_human_sender

DESTINATION_PAIR_MIGRATION_VERSION = 2


def _enabled(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on", "sim"}


class PvReplyWithContacts(PvMessageRuntimeMixin, PvReplyModule):
    """Keep PV business flow intact while layering contacts and editable copy."""

    def __init__(self, storage, settings):
        self.message_store = PvMessageStepStore(storage.pool, settings)
        super().__init__(storage, settings)
        self.contact_ledger = ContactLedger(storage.pool)
        self.auto_save_contacts = _enabled("PV_AUTO_SAVE_CONTACTS")

    async def on_connect(self, client, me) -> None:
        await self.message_store.ensure_ready()
        await self._ensure_destination_pair()
        await super().on_connect(client, me)
        await self.contact_ledger.on_connect(me)

    async def _ensure_destination_pair(self) -> None:
        """Add one editable message before the editable destination exactly once.

        Internal live_* state names stay untouched for compatibility. Operator copy is
        generic: the destination may be a live, VIP, video, group, channel, offer,
        post or any other URL. Deleting either row remains permanent because this
        migration is recorded once and is never re-seeded on restart.
        """
        async with self.storage.pool.acquire() as conn:
            async with conn.transaction():
                applied = await conn.fetchval(
                    "SELECT 1 FROM pv_message_step_migrations WHERE version=$1",
                    DESTINATION_PAIR_MIGRATION_VERSION,
                )
                if applied:
                    return
                await conn.execute(
                    """UPDATE pv_message_steps
                       SET position=$2, label='Destino', updated_at=NOW()
                       WHERE step_key='live.link' AND block_key='live_link'""",
                    POSITION_GAP,
                    POSITION_GAP * 2,
                )
                await conn.execute(
                    """INSERT INTO pv_message_steps(
                       step_key,block_key,branch_key,position,label,content,
                       kind,median_delay_seconds,variant_root,built_in)
                       VALUES(
                         'destination.message','live_link',NULL,$1,
                         'Mensagem do destino','Aqui está 👇','text',0,FALSE,TRUE
                       )
                       ON CONFLICT(step_key) DO NOTHING""",
                    POSITION_GAP,
                )
                await conn.execute(
                    """INSERT INTO pv_message_step_migrations(version)
                       VALUES($1) ON CONFLICT(version) DO NOTHING""",
                    DESTINATION_PAIR_MIGRATION_VERSION,
                )

    def register_actions(self, writer) -> None:
        super().register_actions(writer)
        writer.register(
            self.module_id,
            "ensure_contact_saved",
            self.action_ensure_contact_saved,
        )

    async def handle_event(self, event) -> bool:
        if self.auto_save_contacts and event.is_private and not event.out:
            sender = await event.get_sender()
            if is_human_sender(sender):
                sender_id = int(sender.id)
                if self.me is None or sender_id != int(self.me.id):
                    await self.contact_ledger.queue_save(
                        module_id=self.module_id,
                        user_id=sender_id,
                        username=getattr(sender, "username", None),
                        display_name=display_name(sender),
                        first_name=getattr(sender, "first_name", None),
                        last_name=getattr(sender, "last_name", None),
                        source_type="pv",
                        source_chat_id=None,
                        source_message_id=int(event.id),
                        event_key=self._event_key(event),
                        already_contact=bool(getattr(sender, "contact", False)),
                    )
        return await super().handle_event(event)

    async def action_send_live_link(self, action: dict, effects) -> dict:
        """Deliver the editable message+destination pair.

        Keeping ``{live_link}`` preserves the old campaign destination. Replacing it
        with an explicit URL makes the destination independent from the old live
        campaign link, without changing the existing state machine.
        """
        requires_campaign_link = bool(
            await self.storage.pool.fetchval(
                """SELECT EXISTS(
                   SELECT 1 FROM pv_message_steps
                   WHERE block_key='live_link'
                     AND POSITION('{live_link}' IN content) > 0
                   )"""
            )
        )
        if requires_campaign_link:
            return await super().action_send_live_link(action, effects)

        peer = int(action["payload"]["peer"])
        campaign_id = int(action["payload"]["campaign_id"])
        if not await self.storage.live_recipient_allowed(
            campaign_id, peer, "link_queued"
        ):
            return {"sent": False, "reason": "state_changed"}
        return await self._run_block(
            action=action,
            effects=effects,
            block_key="live_link",
            continuation="live_link",
            context={"peer": peer, "campaign_id": campaign_id},
            variables={},
        )

    async def action_ensure_contact_saved(self, action: dict, effects) -> dict:
        if not self.auto_save_contacts:
            return {"saved": False, "reason": "feature_disabled"}
        return await self.contact_ledger.save_with_effects(action, effects)

    def preview(self) -> str:
        base = super().preview()
        status = "ligado" if self.auto_save_contacts else "desligado"
        return (
            f"{base}\n\nContatos recebidos no PV: autosalvamento {status}."
            "\n\nAs falas são editáveis no Radar por /mensagens_pv."
            "\nMensagem + destino também são editáveis separadamente."
        )
