"""Radar UI mixin for the simple PV speech editor."""

from __future__ import annotations

import math
import re

from telethon import Button, events

from .catalog import match_command
from .human_timing import MIN_WRITING_DELAY_SECONDS, minimum_human_delay_seconds
from .pv_message_steps import BLOCK_LABELS, MAX_MEDIAN_SECONDS

PAGE_SIZE = 5
DISPLAY_BLOCK_LABELS = {**BLOCK_LABELS, "live_link": "Mensagem + destino"}


class PvMessageEditorPanelMixin:
    def __init__(self, application):
        super().__init__(application)
        self.pv_editor_pending: dict | None = None

    def _message_store(self):
        return self.app.registry.get("pv_reply").implementation.message_store

    async def on_message(self, event) -> None:
        if not self.app.is_admin(event):
            return await super().on_message(event)
        if self.pv_editor_pending:
            await self._consume_editor_input(event)
            return
        if match_command(event.raw_text or "", "panel") == "pv_reply.preview":
            await self.show_pv_message_editor(event, 0)
            return
        await super().on_message(event)

    async def on_callback(self, event) -> None:
        if not self.app.is_admin(event):
            return await super().on_callback(event)
        data = event.data.decode("utf-8", errors="replace")
        if data == "command:pv_preview":
            await event.answer()
            await self.show_pv_message_editor(event, 0)
            return
        if data == "pvm:cancel":
            self.pv_editor_pending = None
            await event.answer("Cancelado.", alert=True)
            await self.show_pv_message_editor(event, 0)
            return
        page_match = re.fullmatch(r"pvm:p:(\d+)", data)
        if page_match:
            await event.answer()
            await self.show_pv_message_editor(event, int(page_match.group(1)))
            return
        action_match = re.fullmatch(r"pvm:(text|time|add|empty):(\d+)", data)
        if action_match:
            action, raw_id = action_match.groups()
            step_id = int(raw_id)
            store = self._message_store()
            row = await store.get(step_id)
            if action != "empty" and row is None:
                await event.answer("Essa fala já não existe.", alert=True)
                await self.show_pv_message_editor(event, 0)
                return
            if action == "text":
                self.pv_editor_pending = {"mode": "text", "step_id": step_id}
                await event.answer()
                await event.respond(
                    "Envie o novo conteúdo desta fala.\n\n"
                    "Se quiser apagar definitivamente, use ‘Salvar vazio’.",
                    buttons=[
                        [Button.inline("🗑 Salvar vazio / apagar", f"pvm:empty:{step_id}".encode())],
                        [Button.inline("❌ Cancelar", b"pvm:cancel")],
                    ],
                    parse_mode=None,
                    link_preview=False,
                )
                return
            if action == "empty":
                self.pv_editor_pending = None
                deleted = await store.delete(step_id)
                await event.answer(
                    "Fala apagada definitivamente." if deleted else "A fala já não existia.",
                    alert=True,
                )
                await self.show_pv_message_editor(event, 0)
                return
            if action == "time":
                content = str(row["content"] or "")
                minimum = minimum_human_delay_seconds(content, f"editor:{step_id}")
                self.pv_editor_pending = {
                    "mode": "time",
                    "step_id": step_id,
                    "minimum": minimum,
                }
                await event.answer()
                await event.respond(
                    "Digite a mediana em segundos desde a fala anterior.\n"
                    f"Mínimo humano calculado para esta fala: {minimum} s.\n"
                    "Zero não é permitido.",
                    buttons=[[Button.inline("❌ Cancelar", b"pvm:cancel")]],
                    parse_mode=None,
                    link_preview=False,
                )
                return
            if action == "add":
                self.pv_editor_pending = {"mode": "add_text", "step_id": step_id}
                await event.answer()
                await event.respond(
                    "Envie o conteúdo da nova fala. Ela ficará imediatamente depois desta.",
                    buttons=[[Button.inline("❌ Cancelar", b"pvm:cancel")]],
                    parse_mode=None,
                    link_preview=False,
                )
                return
        await super().on_callback(event)

    async def _consume_editor_input(self, event) -> None:
        pending = dict(self.pv_editor_pending or {})
        mode = pending.get("mode")
        step_id = int(pending.get("step_id", 0))
        store = self._message_store()
        raw = event.raw_text or ""
        if mode == "text":
            self.pv_editor_pending = None
            outcome = await store.set_content(step_id, raw)
            message = {
                "updated": "Fala atualizada.",
                "deleted": "Fala apagada definitivamente.",
                "missing": "Essa fala já não existe.",
            }.get(outcome, outcome)
            await event.respond(message, parse_mode=None)
            await self.show_pv_message_editor(event, await self._page_for_step(step_id))
            return
        if mode == "time":
            value = raw.strip()
            if not value.isdigit():
                await event.respond("Digite somente o número de segundos.", parse_mode=None)
                return
            seconds = int(value)
            minimum = max(MIN_WRITING_DELAY_SECONDS, int(pending.get("minimum", 0)))
            if seconds < minimum:
                await event.respond(
                    f"Tempo muito curto. Para esta fala use no mínimo {minimum} s; zero nunca é permitido.",
                    parse_mode=None,
                )
                return
            if seconds > MAX_MEDIAN_SECONDS:
                await event.respond("Tempo muito alto. Use até 31536000 segundos.", parse_mode=None)
                return
            self.pv_editor_pending = None
            updated = await store.set_delay(step_id, seconds)
            await event.respond(
                "Tempo atualizado." if updated else "Essa fala já não existe.",
                parse_mode=None,
            )
            await self.show_pv_message_editor(event, await self._page_for_step(step_id))
            return
        if mode == "add_text":
            value = raw.strip()
            if not value:
                await event.respond("A nova fala precisa ter conteúdo.", parse_mode=None)
                return
            minimum = minimum_human_delay_seconds(value, f"editor:new:{step_id}")
            self.pv_editor_pending = {
                "mode": "add_time",
                "step_id": step_id,
                "content": value,
                "minimum": minimum,
            }
            await event.respond(
                "Agora digite a mediana em segundos desde a fala anterior.\n"
                f"Mínimo humano para este texto: {minimum} s.\n"
                "Zero não é permitido.",
                buttons=[[Button.inline("❌ Cancelar", b"pvm:cancel")]],
                parse_mode=None,
                link_preview=False,
            )
            return
        if mode == "add_time":
            value = raw.strip()
            if not value.isdigit():
                await event.respond("Digite somente o número de segundos.", parse_mode=None)
                return
            seconds = int(value)
            minimum = max(MIN_WRITING_DELAY_SECONDS, int(pending.get("minimum", 0)))
            if seconds < minimum:
                await event.respond(
                    f"Tempo muito curto. Para esta fala use no mínimo {minimum} s; zero nunca é permitido.",
                    parse_mode=None,
                )
                return
            if seconds > MAX_MEDIAN_SECONDS:
                await event.respond("Tempo muito alto. Use até 31536000 segundos.", parse_mode=None)
                return
            content = str(pending.get("content") or "").strip()
            self.pv_editor_pending = None
            created = await store.add_after(step_id, content, seconds)
            await event.respond(
                "Nova fala criada." if created else "A fala de referência já não existe.",
                parse_mode=None,
            )
            target_id = int(created["id"]) if created else step_id
            await self.show_pv_message_editor(event, await self._page_for_step(target_id))
            return
        self.pv_editor_pending = None
        await event.respond("Edição cancelada: estado desconhecido.", parse_mode=None)

    async def _page_for_step(self, step_id: int) -> int:
        rows = await self._message_store().list_all()
        for index, row in enumerate(rows):
            if int(row["id"]) == int(step_id):
                return index // PAGE_SIZE
        return 0

    async def show_pv_message_editor(self, event, page: int = 0) -> None:
        store = self._message_store()
        rows = await store.list_all()
        pages = max(1, math.ceil(len(rows) / PAGE_SIZE))
        page = max(0, min(int(page), pages - 1))
        start = page * PAGE_SIZE
        current = rows[start : start + PAGE_SIZE]
        lines = [
            "💬 ATENDIMENTO PV — FALAS",
            "",
            "Tempo = mediana em segundos desde a fala anterior.",
            "Leitura + digitação são proporcionais ao texto; tempo zero é proibido.",
            "A variação humana é automática; atrasos já materializados não são recalculados.",
            "",
        ]
        buttons = []
        last_block = None
        for row in current:
            block = str(row["block_key"])
            if block != last_block:
                lines.append(f"— {DISPLAY_BLOCK_LABELS.get(block, block)} —")
                last_block = block
            content = " ".join(str(row["content"]).split())
            if len(content) > 150:
                content = content[:147] + "..."
            lines.append(
                f"#{row['id']} {row['label']} · {int(row['median_delay_seconds'])} s\n{content}"
            )
            step_id = int(row["id"])
            buttons.append(
                [
                    Button.inline("✏️ Texto", f"pvm:text:{step_id}".encode()),
                    Button.inline("⏱ Tempo", f"pvm:time:{step_id}".encode()),
                    Button.inline("➕ Depois", f"pvm:add:{step_id}".encode()),
                ]
            )
        nav = []
        if page > 0:
            nav.append(Button.inline("⬅️", f"pvm:p:{page-1}".encode()))
        nav.append(Button.inline(f"{page+1}/{pages}", f"pvm:p:{page}".encode()))
        if page + 1 < pages:
            nav.append(Button.inline("➡️", f"pvm:p:{page+1}".encode()))
        buttons.append(nav)
        buttons.append([Button.inline("↩️ Funções", b"functions")])
        text = "\n".join(lines)
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
        else:
            await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)
