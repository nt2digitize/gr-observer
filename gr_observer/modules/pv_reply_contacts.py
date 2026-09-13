"""PV contact persistence layered on the existing pv_reply rib."""

from __future__ import annotations

import os

from ..contact_ledger import ContactLedger
from ..pv_message_runtime import PvMessageRuntimeMixin
from ..pv_message_steps import PvMessageStepStore
from .pv_reply import PvReplyModule, display_name, is_human_sender


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
        await super().on_connect(client, me)
        await self.contact_ledger.on_connect(me)

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
        )
