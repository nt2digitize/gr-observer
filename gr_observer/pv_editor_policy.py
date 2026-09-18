"""Canonical non-destructive operator policy for the PV message editor."""

from __future__ import annotations

import re

from telethon import Button

from .catalog import match_command
from .human_timing import MIN_WRITING_DELAY_SECONDS, minimum_human_delay_seconds
from .pv_linear_panel import PvLinearPanelMixin
from .pv_message_panel import PvMessageEditorPanelMixin as _BasePvMessageEditorPanelMixin
from .pv_message_steps import MAX_MEDIAN_SECONDS


_MEDIA_DISPLAY_LABELS = {
    "photo": "📷 Foto",
    "video": "🎬 Vídeo",
    "gif": "🎞 GIF",
}


class _EditorDisplayStore:
    """Read-only display view that labels media-only rows without changing stored copy."""

    def __init__(self, store):
        self._store = store

    def __getattr__(self, name):
        return getattr(self._store, name)

    @staticmethod
    def _row(row):
        if row is None:
            return None
        try:
            content = str(row["content"] or "").strip()
            media_kind = row["media_kind"]
        except (KeyError, TypeError):
            return row
        if content or not media_kind:
            return row
        rendered = dict(row)
        rendered["content"] = _MEDIA_DISPLAY_LABELS.get(
            str(media_kind),
            f"📎 {media_kind}",
        )
        return rendered

    async def get(self, *args, **kwargs):
        return self._row(await self._store.get(*args, **kwargs))

    async def list_all(self, *args, **kwargs):
        return [self._row(row) for row in await self._store.list_all(*args, **kwargs)]

    async def block_rows(self, *args, **kwargs):
        return [self._row(row) for row in await self._store.block_rows(*args, **kwargs)]


class PvEditorPolicyMixin:
    """Editor navigation never mutates copy; explicit input is required to edit."""

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

    @staticmethod
    def _incoming_media_kind(event) -> str | None:
        message = getattr(event, "message", None)
        if message is None:
            return None
        if bool(getattr(message, "gif", False)):
            return "gif"
        if bool(getattr(message, "video", False)):
            return "video"
        if getattr(message, "photo", None) is not None:
            return "photo"
        return None

    @staticmethod
    def _content_buttons(buttons):
        """Keep legacy callbacks while presenting the now-generic content editor."""
        for row in buttons or ():
            for button in row or ():
                if getattr(button, "text", None) == "✏️ Texto":
                    button.text = "📎 Conteúdo"
        return buttons

    def _message_store(self):
        store = super()._message_store()
        if bool(getattr(self, "_pv_media_display_view", False)):
            return _EditorDisplayStore(store)
        return store

    async def show_phase(
        self,
        event,
        block_key: str,
        selected_id: int | None = None,
    ) -> None:
        previous = bool(getattr(self, "_pv_media_display_view", False))
        self._pv_media_display_view = True
        try:
            await super().show_phase(event, block_key, selected_id)
        finally:
            self._pv_media_display_view = previous

    async def show_sequence(
        self,
        event,
        root_id: int,
        selected_id: int | None = None,
    ) -> None:
        previous = bool(getattr(self, "_pv_media_display_view", False))
        self._pv_media_display_view = True
        try:
            await super().show_sequence(event, root_id, selected_id)
        finally:
            self._pv_media_display_view = previous

    async def _render_editor(self, event, text: str, buttons) -> None:
        await super()._render_editor(event, text, self._content_buttons(buttons))

    def _resolved_destination(self, content: str) -> str | None:
        value = content or ""
        if "{preview_link}" in value:
            return str(getattr(self.app.settings, "pv_preview_link", "") or "").strip() or None
        return None

    async def on_callback(self, event) -> None:
        if self.app.is_admin(event) and self.pv_editor_pending:
            data = event.data.decode("utf-8", errors="replace")
            if self._editor_navigation(data):
                self.pv_editor_pending = None
        await super().on_callback(event)

    async def on_message(self, event) -> None:
        if self.app.is_admin(event) and self.pv_editor_pending:
            raw = event.raw_text or ""
            if match_command(raw, "panel"):
                self.pv_editor_pending = None
                await super().on_message(event)
                return
            mode = self.pv_editor_pending.get("mode")
            media_kind = self._incoming_media_kind(event)
            if mode in {"text", "add_text"} and media_kind:
                pending = dict(self.pv_editor_pending)
                source_peer = int(getattr(event, "chat_id", 0) or 0)
                source_message_id = int(getattr(event, "id", 0) or 0)
                if source_peer == 0 or source_message_id <= 0:
                    await event.respond("Não consegui guardar a referência dessa mídia. Nada foi alterado.")
                    return
                caption = raw.strip()
                if mode == "text":
                    outcome = await self._message_store().set_payload(
                        int(pending["step_id"]),
                        caption,
                        media_source_peer=source_peer,
                        media_source_message_id=source_message_id,
                        media_kind=media_kind,
                    )
                    self.pv_editor_pending = None
                    if outcome == "missing":
                        await self.show_phase_index(event, 0)
                        return
                    await self._return_view(event, pending, int(pending["step_id"]))
                    return
                minimum = (
                    minimum_human_delay_seconds(caption, f"editor:new:{pending['step_id']}")
                    if caption
                    else MIN_WRITING_DELAY_SECONDS
                )
                self.pv_editor_pending = {
                    **pending,
                    "mode": "add_time",
                    "content": caption,
                    "minimum": minimum,
                    "media_source_peer": source_peer,
                    "media_source_message_id": source_message_id,
                    "media_kind": media_kind,
                }
                await self._render_editor(
                    event,
                    "⏱ TEMPO DO NOVO BALÃO\n\n"
                    f"Mídia: {media_kind}.\n"
                    f"Mínimo humano: {minimum} s.\n"
                    "Envie a mediana em segundos. Zero não é permitido.",
                    [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
                )
                return
            if mode == "text" and not raw.strip():
                pending = dict(self.pv_editor_pending)
                self.pv_editor_pending = None
                await self._return_view(event, pending, int(pending.get("step_id", 0)))
                return
        await super().on_message(event)

    async def _consume_editor_input(self, event) -> None:
        pending = dict(self.pv_editor_pending or {})
        if pending.get("mode") != "add_time" or pending.get("media_source_message_id") is None:
            await super()._consume_editor_input(event)
            return
        value = (event.raw_text or "").strip()
        if not value.isdigit():
            await self._render_editor(
                event,
                "Digite somente o número de segundos.",
                [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
            )
            return
        seconds = int(value)
        minimum = max(MIN_WRITING_DELAY_SECONDS, int(pending.get("minimum", 0)))
        if seconds < minimum:
            await self._render_editor(
                event,
                f"Tempo muito curto. Use no mínimo {minimum} s; zero nunca é permitido.",
                [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
            )
            return
        if seconds > MAX_MEDIAN_SECONDS:
            await self._render_editor(
                event,
                "Tempo muito alto. Use até 31536000 segundos.",
                [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
            )
            return
        step_id = int(pending.get("step_id", 0))
        created = await self._message_store().add_after(
            step_id,
            str(pending.get("content") or ""),
            seconds,
            media_source_peer=int(pending["media_source_peer"]),
            media_source_message_id=int(pending["media_source_message_id"]),
            media_kind=str(pending["media_kind"]),
        )
        self.pv_editor_pending = None
        target_id = int(created["id"]) if created else step_id
        root_id = pending.get("sequence_root") or step_id
        await self.show_sequence(event, int(root_id), target_id)

    async def _handle_editor_action(
        self,
        event,
        action: str,
        step_id: int,
        *,
        sequence_root: int | None = None,
    ) -> None:
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
        resolved = self._resolved_destination(current)
        destination = f"\n\nDestino que será enviado agora:\n{resolved}" if resolved else ""
        media_kind = row["media_kind"] if "media_kind" in row.keys() else None
        media_line = f"\nMídia atual: {media_kind}" if media_kind else ""
        await self._render_editor(
            event,
            "📎 EDITAR CONTEÚDO\n\n"
            "Conteúdo salvo (copie, edite ou substitua):\n\n"
            f"{current}{destination}{media_line}\n\n"
            "Envie texto/link, foto, vídeo ou GIF. A nova entrada substitui o conteúdo "
            "deste mesmo balão; ID, ordem e tempo permanecem. "
            "Nada muda enquanto você não enviar novo conteúdo.",
            [[Button.inline("✅ Manter atual", b"pvm:cancel")]],
        )


class PvMessageEditorPanel(
    PvLinearPanelMixin,
    PvEditorPolicyMixin,
    _BasePvMessageEditorPanelMixin,
):
    """Final PV editor surface composed statically at import time."""