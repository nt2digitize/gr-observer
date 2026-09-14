"""PV contact persistence layered on the existing pv_reply rib."""

from __future__ import annotations

import os

from ..contact_ledger import ContactLedger
from ..human_timing import (
    MIN_WRITING_DELAY_SECONDS,
    minimum_human_delay_seconds,
    split_human_delay,
)
from ..pv_message_runtime import PvMessageRuntimeMixin
from ..pv_message_steps import (
    POSITION_GAP,
    PvMessageStepStore,
    has_link,
    stable_delay_seconds,
)
from .pv_reply import PvReplyModule, display_name, is_human_sender

DESTINATION_PAIR_MIGRATION_VERSION = 2
HUMAN_TIMING_MIGRATION_VERSION = 3


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
        await self._ensure_human_timing_floor()
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
                       SET position=$1, label='Destino', updated_at=NOW()
                       WHERE step_key='live.link' AND block_key='live_link'""",
                    POSITION_GAP * 2,
                )
                await conn.execute(
                    """INSERT INTO pv_message_steps(
                       step_key,block_key,branch_key,position,label,content,
                       kind,median_delay_seconds,variant_root,built_in)
                       VALUES(
                         'destination.message','live_link',NULL,$1,
                         'Mensagem do destino','Aqui está 👇','text',$2,FALSE,TRUE
                       )
                       ON CONFLICT(step_key) DO NOTHING""",
                    POSITION_GAP,
                    MIN_WRITING_DELAY_SECONDS,
                )
                await conn.execute(
                    """INSERT INTO pv_message_step_migrations(version)
                       VALUES($1) ON CONFLICT(version) DO NOTHING""",
                    DESTINATION_PAIR_MIGRATION_VERSION,
                )

    async def _ensure_human_timing_floor(self) -> None:
        """Remove legacy zero/near-zero message medians exactly once.

        Runtime still applies a proportional floor based on the actual rendered text;
        this migration guarantees the persisted operator value itself is never zero.
        """
        async with self.storage.pool.acquire() as conn:
            async with conn.transaction():
                applied = await conn.fetchval(
                    "SELECT 1 FROM pv_message_step_migrations WHERE version=$1",
                    HUMAN_TIMING_MIGRATION_VERSION,
                )
                if applied:
                    return
                await conn.execute(
                    """UPDATE pv_message_steps
                       SET median_delay_seconds=$1,updated_at=NOW()
                       WHERE median_delay_seconds<$1""",
                    MIN_WRITING_DELAY_SECONDS,
                )
                await conn.execute(
                    """INSERT INTO pv_message_step_migrations(version)
                       VALUES($1) ON CONFLICT(version) DO NOTHING""",
                    HUMAN_TIMING_MIGRATION_VERSION,
                )

    async def _first_step_plan(
        self,
        block_key: str,
        stable_key: str,
        *,
        extra_seconds: int = 0,
    ):
        """Use the exact same deterministic key later used by the actual send."""
        await self.message_store.ensure_ready()
        branch = await self.message_store.choose_branch(block_key, stable_key)
        row = await self.message_store.first_step(block_key, branch)
        if row is None:
            return branch, None, 0.0, 0.0
        rendered = self.message_store.render(
            str(row["content"]), preview_link=self.settings.pv_preview_link
        )
        timing_key = f"{stable_key}:step:{row['id']}"
        proportional_floor = minimum_human_delay_seconds(rendered, timing_key)
        median = max(
            proportional_floor,
            int(row["median_delay_seconds"]) + int(extra_seconds),
        )
        total = max(
            proportional_floor,
            stable_delay_seconds(timing_key, median),
        )
        prewait, typing = split_human_delay(total, rendered, timing_key)
        return branch, row, prewait, typing

    async def _send_row(
        self,
        *,
        effects,
        peer: int,
        row,
        origin_key: str,
        variables: dict,
    ) -> dict:
        """Every PV text gets proportional reading time plus visible typing."""
        text = self.message_store.render(
            str(row["content"]),
            preview_link=self.settings.pv_preview_link,
            live_link=str(variables.get("live_link") or ""),
        ).strip()
        if not text:
            return {
                "sent": False,
                "reason": "empty_after_render",
                "step_id": int(row["id"]),
            }
        timing_key = f"{origin_key}:step:{row['id']}"
        proportional_floor = minimum_human_delay_seconds(text, timing_key)
        median = max(proportional_floor, int(row["median_delay_seconds"]))
        total = max(proportional_floor, stable_delay_seconds(timing_key, median))
        _, typing = split_human_delay(total, text, timing_key)
        await self._show_typing(effects, peer, typing)
        effect_key = f"{origin_key}:message:{row['id']}"
        sender = (
            effects.send_text_preview
            if has_link(text) or str(row["kind"]) == "link"
            else effects.send_text
        )
        result = await sender(peer, text, effect_key)
        return {"sent": True, "step_id": int(row["id"]), **result}

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
            "\nToda fala automática usa leitura + digitação proporcional; tempo zero é bloqueado."
        )
