"""Operator editor for the single-line PV conversation."""

from __future__ import annotations

import re

from telethon import Button

from .catalog import match_command
from .human_timing import MIN_WRITING_DELAY_SECONDS, minimum_human_delay_seconds
from .pv_message_steps import MAX_MEDIAN_SECONDS

_MEDIA_LABELS = {
    "photo": "📷 Foto",
    "video": "🎬 Vídeo",
    "gif": "🎞 GIF",
    "sticker": "🙂 Sticker",
    "voice": "🎙 Voz",
    "audio": "🎵 Áudio",
    "document": "📎 Arquivo",
    "media": "📎 Mídia",
}


class PvLinearPanelMixin:
    """Frames are context labels; balloons remain one globally ordered line."""

    def _linear_runtime(self):
        return self.app.registry.get("pv_reply").implementation

    async def _linear_stores(self):
        runtime = self._linear_runtime()
        await runtime.message_store.ensure_ready()
        await runtime.linear_store.ensure_ready()
        return runtime.linear_store, runtime.message_store

    @staticmethod
    def _linear_media_kind(event) -> str | None:
        message = getattr(event, "message", None)
        if message is None:
            return None
        if bool(getattr(message, "gif", False)):
            return "gif"
        if bool(getattr(message, "video", False)):
            return "video"
        if getattr(message, "photo", None) is not None:
            return "photo"
        if bool(getattr(message, "sticker", False)):
            return "sticker"
        if bool(getattr(message, "voice", False)):
            return "voice"
        if bool(getattr(message, "audio", False)):
            return "audio"
        if getattr(message, "document", None) is not None:
            return "document"
        if getattr(message, "media", None) is not None:
            return "media"
        return None

    @staticmethod
    def _linear_row_text(row) -> str:
        content = " ".join(str(row["content"] or "").split())
        if content:
            return content
        kind = str(row["media_kind"] or "media") if "media_kind" in row.keys() else "media"
        return _MEDIA_LABELS.get(kind, f"📎 {kind}")

    async def on_callback(self, event) -> None:
        if not self.app.is_admin(event):
            return await super().on_callback(event)
        data = event.data.decode("utf-8", errors="replace")

        if data in {"command:pv_preview", "pvl:home"}:
            self.pv_editor_pending = None
            await event.answer()
            await self.show_linear_conversation(event)
            return
        if data == "pvl:cancel":
            self.pv_editor_pending = None
            await event.answer("Cancelado.")
            await self.show_linear_conversation(event)
            return
        if data == "pvl:newphase":
            self.pv_editor_pending = {"mode": "linear_phase_label"}
            await event.answer()
            await self._render_editor(
                event,
                "🗂 NOVO QUADRO\n\nEnvie somente o nome do quadro.",
                [[Button.inline("❌ Cancelar", b"pvl:cancel")]],
            )
            return

        select = re.fullmatch(r"pvl:s:(\d+)", data)
        if select:
            await event.answer()
            await self.show_linear_conversation(event, int(select.group(1)))
            return

        toggle = re.fullmatch(r"pvl:wait:(\d+)", data)
        if toggle:
            step_id = int(toggle.group(1))
            linear, _ = await self._linear_stores()
            row = await linear.get_step(step_id)
            if row is None:
                await event.answer("Balão não encontrado.", alert=True)
                await self.show_linear_conversation(event)
                return
            requested = not bool(row["wait_for_reply"])
            changed = await linear.set_wait_for_reply(step_id, requested)
            if requested and not changed:
                await event.answer(
                    "Antes do link da prévia, a conversa continua automaticamente.",
                    alert=True,
                )
            else:
                await event.answer("Regra atualizada.")
            await self.show_linear_conversation(event, step_id)
            return

        move = re.fullmatch(r"pvl:(up|down):(\d+)", data)
        if move:
            direction, raw_id = move.groups()
            step_id = int(raw_id)
            linear, _ = await self._linear_stores()
            changed = await linear.move(step_id, -1 if direction == "up" else 1)
            await event.answer("Movido." if changed else "Já está no limite.")
            await self.show_linear_conversation(event, step_id)
            return

        phase = re.fullmatch(r"pvl:phase:(\d+)", data)
        if phase:
            await event.answer()
            await self.show_linear_phase_picker(event, int(phase.group(1)))
            return

        set_phase = re.fullmatch(r"pvl:setphase:(\d+):([a-z0-9_]+)", data)
        if set_phase:
            step_id = int(set_phase.group(1))
            phase_key = set_phase.group(2)
            linear, _ = await self._linear_stores()
            await linear.set_phase(step_id, phase_key)
            await event.answer("Quadro alterado.")
            await self.show_linear_conversation(event, step_id)
            return

        content = re.fullmatch(r"pvl:content:(\d+)", data)
        if content:
            step_id = int(content.group(1))
            linear, _ = await self._linear_stores()
            row = await linear.get_step(step_id)
            if row is None:
                await event.answer("Balão não encontrado.", alert=True)
                await self.show_linear_conversation(event)
                return
            self.pv_editor_pending = {"mode": "linear_content", "step_id": step_id}
            await event.answer()
            await self._render_editor(
                event,
                "📎 CONTEÚDO DO BALÃO\n\n"
                f"Atual: {self._linear_row_text(row)}\n\n"
                "Envie texto, link, foto, vídeo, GIF, sticker, áudio/voz ou arquivo. "
                "A nova mensagem substitui o conteúdo deste mesmo balão.",
                [[Button.inline("✅ Manter atual", b"pvl:cancel")]],
            )
            return

        timing = re.fullmatch(r"pvl:time:(\d+)", data)
        if timing:
            step_id = int(timing.group(1))
            linear, _ = await self._linear_stores()
            row = await linear.get_step(step_id)
            if row is None:
                await event.answer("Balão não encontrado.", alert=True)
                return
            content_text = str(row["content"] or "")
            minimum = (
                minimum_human_delay_seconds(content_text, f"linear:time:{step_id}")
                if content_text.strip()
                else MIN_WRITING_DELAY_SECONDS
            )
            self.pv_editor_pending = {
                "mode": "linear_time",
                "step_id": step_id,
                "minimum": minimum,
            }
            await event.answer()
            await self._render_editor(
                event,
                "⏱ ESPERAR ANTES DE ENVIAR\n\n"
                f"Atual: {int(row['median_delay_seconds'])} s\n"
                f"Mínimo humano: {minimum} s.\n\n"
                "Envie o novo número de segundos. Este relógio começa quando este balão é liberado.",
                [[Button.inline("❌ Cancelar", b"pvl:cancel")]],
            )
            return

        add = re.fullmatch(r"pvl:add:(\d+)", data)
        if add:
            after_id = int(add.group(1))
            self.pv_editor_pending = {"mode": "linear_add_content", "after_id": after_id}
            await event.answer()
            await self._render_editor(
                event,
                "➕ NOVO BALÃO\n\n"
                "Envie o conteúdo. Pode ser texto, link ou mídia do Telegram.",
                [[Button.inline("❌ Cancelar", b"pvl:cancel")]],
            )
            return

        delete = re.fullmatch(r"pvl:delete:(\d+)", data)
        if delete:
            step_id = int(delete.group(1))
            await event.answer()
            await self._render_editor(
                event,
                "🗑 REMOVER BALÃO DA CONVERSA?\n\nEle sai da linha nova sem apagar histórico antigo.",
                [
                    [Button.inline("🗑 Sim, remover", f"pvl:confirmdel:{step_id}".encode())],
                    [Button.inline("❌ Cancelar", b"pvl:cancel")],
                ],
            )
            return

        confirm = re.fullmatch(r"pvl:confirmdel:(\d+)", data)
        if confirm:
            step_id = int(confirm.group(1))
            linear, _ = await self._linear_stores()
            await linear.remove_from_line(step_id)
            self.pv_editor_pending = None
            await event.answer("Balão removido da linha.")
            await self.show_linear_conversation(event)
            return

        await super().on_callback(event)

    async def on_message(self, event) -> None:
        if self.app.is_admin(event):
            raw = event.raw_text or ""
            if match_command(raw, "panel") == "pv_reply.preview":
                self.pv_editor_pending = None
                await self.show_linear_conversation(event)
                return
            pending = dict(self.pv_editor_pending or {})
            mode = str(pending.get("mode") or "")
            if mode.startswith("linear_"):
                await self._consume_linear_input(event, pending)
                return
        await super().on_message(event)

    async def _consume_linear_input(self, event, pending: dict) -> None:
        mode = str(pending.get("mode") or "")
        raw = event.raw_text or ""
        linear, messages = await self._linear_stores()
        media_kind = self._linear_media_kind(event)

        if mode == "linear_content":
            step_id = int(pending["step_id"])
            if media_kind:
                source_peer = int(getattr(event, "chat_id", 0) or 0)
                source_message_id = int(getattr(event, "id", 0) or 0)
                if source_peer == 0 or source_message_id <= 0:
                    await event.respond("Não consegui guardar a referência dessa mídia.")
                    return
                await messages.set_payload(
                    step_id,
                    raw.strip(),
                    media_source_peer=source_peer,
                    media_source_message_id=source_message_id,
                    media_kind=media_kind,
                )
            elif raw.strip():
                await messages.set_content(step_id, raw.strip())
            else:
                await event.respond("Conteúdo vazio não altera o balão.")
                return
            self.pv_editor_pending = None
            await self.show_linear_conversation(event, step_id)
            return

        if mode == "linear_time":
            value = raw.strip()
            if not value.isdigit():
                await event.respond("Envie somente o número de segundos.")
                return
            seconds = int(value)
            minimum = max(MIN_WRITING_DELAY_SECONDS, int(pending.get("minimum", 0)))
            if seconds < minimum or seconds > MAX_MEDIAN_SECONDS:
                await event.respond(f"Use um valor entre {minimum} e {MAX_MEDIAN_SECONDS} segundos.")
                return
            await messages.set_delay(int(pending["step_id"]), seconds)
            step_id = int(pending["step_id"])
            self.pv_editor_pending = None
            await self.show_linear_conversation(event, step_id)
            return

        if mode == "linear_add_content":
            value = raw.strip()
            if not value and not media_kind:
                await event.respond("O novo balão precisa ter conteúdo.")
                return
            minimum = (
                minimum_human_delay_seconds(value, f"linear:new:{pending.get('after_id', 0)}")
                if value
                else MIN_WRITING_DELAY_SECONDS
            )
            new_pending = {
                **pending,
                "mode": "linear_add_time",
                "content": value,
                "minimum": minimum,
            }
            if media_kind:
                new_pending.update(
                    {
                        "media_kind": media_kind,
                        "media_source_peer": int(getattr(event, "chat_id", 0) or 0),
                        "media_source_message_id": int(getattr(event, "id", 0) or 0),
                    }
                )
            self.pv_editor_pending = new_pending
            await self._render_editor(
                event,
                "⏱ ESPERA DO NOVO BALÃO\n\n"
                f"Mínimo humano: {minimum} s.\n"
                "Envie quantos segundos esperar ANTES de enviar esse balão.",
                [[Button.inline("❌ Cancelar", b"pvl:cancel")]],
            )
            return

        if mode == "linear_add_time":
            value = raw.strip()
            if not value.isdigit():
                await event.respond("Envie somente o número de segundos.")
                return
            seconds = int(value)
            minimum = max(MIN_WRITING_DELAY_SECONDS, int(pending.get("minimum", 0)))
            if seconds < minimum or seconds > MAX_MEDIAN_SECONDS:
                await event.respond(f"Use um valor entre {minimum} e {MAX_MEDIAN_SECONDS} segundos.")
                return
            created = await linear.add_after(
                int(pending.get("after_id") or 0) or None,
                content=str(pending.get("content") or ""),
                median_delay_seconds=seconds,
                media_source_peer=pending.get("media_source_peer"),
                media_source_message_id=pending.get("media_source_message_id"),
                media_kind=pending.get("media_kind"),
            )
            self.pv_editor_pending = None
            await self.show_linear_conversation(event, int(created["id"]))
            return

        if mode == "linear_phase_label":
            label = raw.strip()
            if not label:
                await event.respond("O quadro precisa ter um nome.")
                return
            self.pv_editor_pending = {"mode": "linear_phase_instruction", "label": label}
            await self._render_editor(
                event,
                "🗂 INSTRUÇÃO DO QUADRO\n\n"
                f"Quadro: {label}\n\n"
                "Em uma frase, diga o objetivo desta fase da conversa.",
                [[Button.inline("❌ Cancelar", b"pvl:cancel")]],
            )
            return

        if mode == "linear_phase_instruction":
            await linear.add_phase(str(pending["label"]), raw.strip())
            self.pv_editor_pending = None
            await self.show_linear_conversation(event)
            return

        self.pv_editor_pending = None
        await self.show_linear_conversation(event)

    async def show_linear_phase_picker(self, event, step_id: int) -> None:
        linear, _ = await self._linear_stores()
        phases = await linear.phases()
        buttons = [
            [Button.inline(str(row["label"]), f"pvl:setphase:{step_id}:{row['phase_key']}".encode())]
            for row in phases
        ]
        buttons.append([Button.inline("↩️ Voltar", f"pvl:s:{step_id}".encode())])
        await self._render_editor(
            event,
            "🗂 MOVER BALÃO PARA QUAL QUADRO?",
            buttons,
        )

    async def show_linear_conversation(self, event, selected_id: int | None = None) -> None:
        linear, _ = await self._linear_stores()
        phases = list(await linear.phases())
        steps = list(await linear.steps())
        phase_map = {str(row["phase_key"]): row for row in phases}
        selected = next(
            (row for row in steps if int(row["id"]) == int(selected_id or 0)),
            steps[0] if steps else None,
        )
        selected_id = int(selected["id"]) if selected is not None else 0

        lines = [
            "🧵 CONVERSA PV — LINHA ÚNICA",
            "",
            "Os quadros só mostram o contexto. A conversa segue balão por balão.",
            "⏱ = esperar antes de enviar | ⏸ = aguardar resposta depois",
        ]
        last_phase = None
        for index, row in enumerate(steps, start=1):
            phase_key = str(row["phase_key"] or "")
            if phase_key != last_phase:
                phase = phase_map.get(phase_key)
                lines += ["", f"▰ {str(phase['label']) if phase else phase_key}"]
                if phase and str(phase["instruction"] or "").strip():
                    lines.append(f"↳ {str(phase['instruction']).strip()}")
                last_phase = phase_key
            marker = "✅" if int(row["id"]) == selected_id else "○"
            gate = "⏸" if bool(row["wait_for_reply"]) else "➡️"
            text = self._linear_row_text(row)
            if len(text) > 70:
                text = text[:67] + "..."
            lines.append(
                f"{marker} {index}. {gate} {text}  ·  ⏱ {int(row['median_delay_seconds'])}s"
            )

        if selected is not None:
            selected_index = next(
                i for i, row in enumerate(steps, start=1) if int(row["id"]) == selected_id
            )
            phase = phase_map.get(str(selected["phase_key"] or ""))
            lines += [
                "",
                "────────────",
                f"BALÃO {selected_index} DE {len(steps)}",
                self._linear_row_text(selected),
                f"Quadro: {str(phase['label']) if phase else selected['phase_key']}",
                f"⏱ Esperar antes de enviar: {int(selected['median_delay_seconds'])} s",
                (
                    "⏸ Depois de enviar: AGUARDAR RESPOSTA"
                    if bool(selected["wait_for_reply"])
                    else "➡️ Depois de enviar: CONTINUAR AUTOMATICAMENTE"
                ),
            ]

        select_buttons = []
        row_buttons = []
        for index, row in enumerate(steps, start=1):
            label = f"✅ {index}" if int(row["id"]) == selected_id else str(index)
            row_buttons.append(Button.inline(label, f"pvl:s:{int(row['id'])}".encode()))
            if len(row_buttons) == 5:
                select_buttons.append(row_buttons)
                row_buttons = []
        if row_buttons:
            select_buttons.append(row_buttons)

        buttons = list(select_buttons)
        if selected is not None:
            buttons += [
                [
                    Button.inline("📎 Conteúdo", f"pvl:content:{selected_id}".encode()),
                    Button.inline("⏱ Espera", f"pvl:time:{selected_id}".encode()),
                ],
                [
                    Button.inline(
                        "⏸ Resposta: SIM" if bool(selected["wait_for_reply"]) else "➡️ Resposta: NÃO",
                        f"pvl:wait:{selected_id}".encode(),
                    ),
                    Button.inline("🗂 Quadro", f"pvl:phase:{selected_id}".encode()),
                ],
                [
                    Button.inline("⬆️ Subir", f"pvl:up:{selected_id}".encode()),
                    Button.inline("⬇️ Descer", f"pvl:down:{selected_id}".encode()),
                ],
                [
                    Button.inline("➕ Balão depois", f"pvl:add:{selected_id}".encode()),
                    Button.inline("🗑 Remover", f"pvl:delete:{selected_id}".encode()),
                ],
            ]
        else:
            buttons.append([Button.inline("➕ Primeiro balão", b"pvl:add:0")])
        buttons += [
            [Button.inline("➕ Novo quadro", b"pvl:newphase")],
            [Button.inline("↩️ Atendimento PV", b"menu:pv")],
        ]
        await self._render_editor(event, "\n".join(lines)[:3900], buttons)