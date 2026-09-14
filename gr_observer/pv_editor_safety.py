"""Non-destructive operator UX for the PV message editor."""

from __future__ import annotations

import re

from telethon import Button, events

from .catalog import match_command


class PvEditorSafetyMixin:
    """Opening or leaving an editor never mutates production copy."""

    @staticmethod
    def _editor_navigation(data: str) -> bool:
        if data in {"pvm:home", "command:pv_preview", "menu:pv"}:
            return True
        return bool(
            re.fullmatch(r"pvm:(?:ph|p):\d+", data)
            or re.fullmatch(r"pvm:b:[a-z_]+", data)
            or re.fullmatch(r"pvm:s:\d+", data)
            or re.fullmatch(r"pvm:seq:\d+", data)
            or re.fullmatch(r"pvm:q:\d+:\d+", data)
        )

    async def on_callback(self, event) -> None:
        if self.app.is_admin(event) and self.pv_editor_pending:
            data = event.data.decode("utf-8", errors="replace")
            if self._editor_navigation(data):
                # Navigation means "leave without saving".  Production copy stays intact.
                self.pv_editor_pending = None
        await super().on_callback(event)

    async def on_message(self, event) -> None:
        if self.app.is_admin(event) and self.pv_editor_pending:
            raw = event.raw_text or ""
            # A real panel command is navigation, not replacement copy.
            if match_command(raw, "panel"):
                self.pv_editor_pending = None
                await super().on_message(event)
                return
            # Telegram cannot send an empty text message, but a photo/sticker while
            # editing has empty raw_text.  Never interpret that as "delete".
            if self.pv_editor_pending.get("mode") == "text" and not raw.strip():
                pending = dict(self.pv_editor_pending)
                self.pv_editor_pending = None
                await self._return_view(event, pending, int(pending.get("step_id", 0)))
                return
        await super().on_message(event)

    async def _handle_editor_action(
        self,
        event,
        action: str,
        step_id: int,
        *,
        sequence_root: int | None = None,
    ) -> None:
        # Legacy "empty" callbacks used to delete the row.  Deletion must now be
        # explicit and confirmed through the dedicated Excluir button only.
        if action == "empty":
            row = await self._message_store().get(step_id)
            if row is None:
                await event.answer("Essa fala já não existe.", alert=True)
                await self.show_phase_index(event, 0)
                return
            await event.answer("Mantida sem alteração.")
            pending = {
                "step_id": step_id,
                "block_key": str(row["block_key"]),
                "sequence_root": sequence_root,
            }
            self.pv_editor_pending = None
            await self._return_view(event, pending, step_id)
            return

        if action != "text":
            await super()._handle_editor_action(
                event, action, step_id, sequence_root=sequence_root
            )
            return

        row = await self._message_store().get(step_id)
        if row is None:
            await event.answer("Essa fala já não existe.", alert=True)
            await self.show_phase_index(event, 0)
            return
        self.pv_editor_pending = {
            "step_id": step_id,
            "block_key": str(row["block_key"]),
            "sequence_root": sequence_root,
            "mode": "text",
        }
        await event.answer()
        current = str(row["content"] or "")
        await self._render_editor(
            event,
            "✏️ EDITAR FALA\n\n"
            "Texto atual (copie, edite e envie):\n\n"
            f"{current}\n\n"
            "Nada muda enquanto você não enviar um novo texto. "
            "Sair ou tocar em Manter atual preserva a produção.",
            [[Button.inline("✅ Manter atual", b"pvm:cancel")]],
        )
