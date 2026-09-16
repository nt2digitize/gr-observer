"""Context-first operator UI for the configurable PV speech editor."""

from __future__ import annotations

import math
import re

from telethon import Button, events

from .catalog import match_command
from .human_timing import MIN_WRITING_DELAY_SECONDS, minimum_human_delay_seconds
from .pv_message_steps import BLOCK_LABELS, BLOCK_ORDER, MAX_MEDIAN_SECONDS

PHASE_PAGE_SIZE = 6
DISPLAY_BLOCK_LABELS = {**BLOCK_LABELS, "live_link": "Mensagem + destino"}

UI_META_DDL = """
CREATE TABLE IF NOT EXISTS pv_message_step_ui_meta (
    step_id BIGINT PRIMARY KEY REFERENCES pv_message_steps(id) ON DELETE CASCADE,
    intent TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""

PHASE_CONTEXT = {
    "greeting": (
        "Abrir a conversa com curiosidade e obter uma resposta curta.",
        "Entra no primeiro contato do PV. Depois segue para o convite da prévia.",
    ),
    "link": (
        "Transformar interesse em acesso à prévia.",
        "Entra depois da abertura. Conduz a conversa até o destino de prévia.",
    ),
    "followup": (
        "Retomar quem recebeu a prévia sem travar a fila global.",
        "Entra após a prévia conforme a cadência persistida. Depois continua a jornada.",
    ),
    "reminder_link": (
        "Reapresentar o destino quando a resposta indicar que o acesso ainda é necessário.",
        "É um reenvio contextual do link, sem reiniciar a jornada.",
    ),
    "weekly": (
        "Reativar contato antigo com contexto suficiente para retomar a conversa.",
        "Entra no ciclo semanal persistido e pode reconduzir para a prévia.",
    ),
    "live_optin": (
        "Pedir consentimento para avisos de live.",
        "Entra antes de qualquer convite de live e registra a decisão do contato.",
    ),
    "live_invite": (
        "Convidar quem consentiu quando houver uma live ativa.",
        "Entra somente em campanha de live e conduz para o destino configurado.",
    ),
    "live_remarketing": (
        "Relembrar a oportunidade de live sem repetir exatamente a mesma abordagem.",
        "Entra na cadência de remarketing da campanha ativa.",
    ),
    "live_link": (
        "Entregar a mensagem e o destino da campanha ativa.",
        "Vem após aceite/continuação da campanha de live.",
    ),
    "two_screens_prompt": (
        "Introduzir a dinâmica de duas telas.",
        "Entra depois do avanço da jornada e prepara a escolha de preferência.",
    ),
    "two_screens_preference": (
        "Coletar uma preferência curta para personalizar a próxima ação.",
        "Entra após a abertura de duas telas e antecede a mídia correspondente.",
    ),
    "two_screens_question": (
        "Manter compatibilidade com a pergunta legada de duas telas.",
        "Só é usada quando o fluxo legado estiver ativo.",
    ),
    "two_screens_limit": (
        "Explicar o limite de escolha sem interromper a conversa.",
        "Entra quando é necessário restringir a seleção a uma opção.",
    ),
    "two_screens_followup": (
        "Manter continuidade depois da escolha/mídia.",
        "Entra após o envio relacionado à preferência selecionada.",
    ),
    "two_screens_retry_preference": (
        "Pedir novamente uma preferência válida de forma curta.",
        "Entra quando a resposta anterior não permite classificar a preferência.",
    ),
    "two_screens_retry": (
        "Manter compatibilidade com o retry legado.",
        "Só é usada no fluxo legado quando a resposta não é reconhecida.",
    ),
    "photo_caption_peitos": (
        "Contextualizar a mídia correspondente à preferência peitos.",
        "É enviada junto da mídia escolhida, respeitando a cadência humana.",
    ),
    "photo_caption_buceta": (
        "Contextualizar a mídia correspondente à preferência buceta.",
        "É enviada junto da mídia escolhida, respeitando a cadência humana.",
    ),
    "photo_caption_cu": (
        "Contextualizar a mídia correspondente à preferência cu.",
        "É enviada junto da mídia escolhida, respeitando a cadência humana.",
    ),
}


class PvMessageEditorPanelMixin:
    def __init__(self, application):
        super().__init__(application)
        self.pv_editor_pending: dict | None = None
        self.pv_editor_anchor: tuple[int, int] | None = None
        self._pv_ui_meta_ready = False

    def _message_store(self):
        return self.app.registry.get("pv_reply").implementation.message_store

    async def _ensure_ui_meta(self) -> None:
        if self._pv_ui_meta_ready:
            return
        await self._message_store().ensure_ready()
        await self.app.pool.execute(UI_META_DDL)
        self._pv_ui_meta_ready = True

    async def _get_intent(self, step_id: int, block_key: str) -> str:
        await self._ensure_ui_meta()
        value = await self.app.pool.fetchval(
            "SELECT intent FROM pv_message_step_ui_meta WHERE step_id=$1",
            int(step_id),
        )
        if value and str(value).strip():
            return str(value).strip()
        phase_intent, _ = PHASE_CONTEXT.get(
            block_key,
            ("Cumprir a função desta fase da jornada.", "Mantém a continuidade do fluxo."),
        )
        return f"Variação desta fase para: {phase_intent[0].lower() + phase_intent[1:]}"

    async def _set_intent(self, step_id: int, value: str) -> None:
        await self._ensure_ui_meta()
        await self.app.pool.execute(
            """INSERT INTO pv_message_step_ui_meta(step_id,intent,updated_at)
               VALUES($1,$2,NOW())
               ON CONFLICT(step_id) DO UPDATE
               SET intent=EXCLUDED.intent,updated_at=NOW()""",
            int(step_id),
            value.strip(),
        )

    async def _remember_anchor(self, event) -> None:
        chat_id = getattr(event, "chat_id", None)
        message_id = getattr(event, "message_id", None)
        if message_id is None and isinstance(event, events.CallbackQuery.Event):
            try:
                message = await event.get_message()
            except Exception:
                message = None
            message_id = getattr(message, "id", None)
        if chat_id is not None and message_id is not None:
            self.pv_editor_anchor = (int(chat_id), int(message_id))

    async def _render_editor(self, event, text: str, buttons) -> None:
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
            await self._remember_anchor(event)
            return
        if self.pv_editor_anchor is not None:
            chat_id, message_id = self.pv_editor_anchor
            try:
                await self.client.edit_message(
                    chat_id,
                    message_id,
                    text,
                    buttons=buttons,
                    parse_mode=None,
                    link_preview=False,
                )
                return
            except Exception:
                self.pv_editor_anchor = None
        sent = await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)
        self.pv_editor_anchor = (int(sent.chat_id), int(sent.id))

    async def on_message(self, event) -> None:
        if not self.app.is_admin(event):
            return await super().on_message(event)
        if self.pv_editor_pending:
            await self._consume_editor_input(event)
            return
        if match_command(event.raw_text or "", "panel") == "pv_reply.preview":
            self.pv_editor_anchor = None
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
        if data == "pvm:home":
            await event.answer()
            await self.show_phase_index(event, 0)
            return
        if data == "pvm:cancel":
            pending = dict(self.pv_editor_pending or {})
            self.pv_editor_pending = None
            await event.answer("Cancelado.")
            await self._return_view(event, pending)
            return

        page_match = re.fullmatch(r"pvm:(?:ph|p):(\d+)", data)
        if page_match:
            await event.answer()
            await self.show_phase_index(event, int(page_match.group(1)))
            return

        block_match = re.fullmatch(r"pvm:b:([a-z_]+)", data)
        if block_match:
            await event.answer()
            await self.show_phase(event, block_match.group(1))
            return

        select_match = re.fullmatch(r"pvm:s:(\d+)", data)
        if select_match:
            row = await self._message_store().get(int(select_match.group(1)))
            if row is None:
                await event.answer("Essa fala já não existe.", alert=True)
                await self.show_phase_index(event, 0)
                return
            await event.answer()
            await self.show_phase(event, str(row["block_key"]), int(row["id"]))
            return

        sequence_match = re.fullmatch(r"pvm:seq:(\d+)", data)
        if sequence_match:
            step_id = int(sequence_match.group(1))
            await event.answer()
            await self.show_sequence(event, step_id, step_id)
            return

        sequence_select = re.fullmatch(r"pvm:q:(\d+):(\d+)", data)
        if sequence_select:
            root_id, selected_id = map(int, sequence_select.groups())
            await event.answer()
            await self.show_sequence(event, root_id, selected_id)
            return

        sequence_action = re.fullmatch(
            r"pvmx:(text|time|intent|add|delete|empty):(\d+):(\d+)", data
        )
        if sequence_action:
            action, raw_root, raw_id = sequence_action.groups()
            await self._handle_editor_action(
                event,
                action,
                int(raw_id),
                sequence_root=int(raw_root),
            )
            return

        action_match = re.fullmatch(
            r"pvm:(text|time|intent|add|delete|empty):(\d+)", data
        )
        if action_match:
            action, raw_id = action_match.groups()
            await self._handle_editor_action(event, action, int(raw_id))
            return

        confirm_sequence = re.fullmatch(r"pvmx:confirm:(\d+):(\d+)", data)
        if confirm_sequence:
            root_id, step_id = map(int, confirm_sequence.groups())
            await self._delete_step(event, step_id, sequence_root=root_id)
            return

        confirm = re.fullmatch(r"pvm:confirm:(\d+)", data)
        if confirm:
            await self._delete_step(event, int(confirm.group(1)))
            return

        await super().on_callback(event)

    async def _handle_editor_action(
        self,
        event,
        action: str,
        step_id: int,
        *,
        sequence_root: int | None = None,
    ) -> None:
        store = self._message_store()
        row = await store.get(step_id)
        if row is None:
            await event.answer("Essa fala já não existe.", alert=True)
            await self.show_phase_index(event, 0)
            return
        block_key = str(row["block_key"])
        pending_base = {
            "step_id": step_id,
            "block_key": block_key,
            "sequence_root": sequence_root,
        }

        if action == "empty":
            await self._delete_step(event, step_id, sequence_root=sequence_root)
            return
        if action == "delete":
            await event.answer()
            if sequence_root is None:
                confirm_data = f"pvm:confirm:{step_id}".encode()
            else:
                confirm_data = f"pvmx:confirm:{sequence_root}:{step_id}".encode()
            await self._render_editor(
                event,
                "🗑 EXCLUIR FALA\n\n"
                f"“{str(row['content']).strip()}”\n\n"
                "Essa ação apaga somente esta fala. Confirmar?",
                [
                    [Button.inline("🗑 Sim, excluir", confirm_data)],
                    [Button.inline("❌ Cancelar", b"pvm:cancel")],
                ],
            )
            self.pv_editor_pending = {**pending_base, "mode": "confirm_delete"}
            return
        if action == "text":
            self.pv_editor_pending = {**pending_base, "mode": "text"}
            await event.answer()
            await self._render_editor(
                event,
                "✏️ ALTERAR FALA\n\n"
                f"Atual:\n“{str(row['content']).strip()}”\n\n"
                "Envie o novo texto. Ele SUBSTITUI esta fala; não cria outra.",
                [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
            )
            return
        if action == "time":
            content = str(row["content"] or "")
            minimum = minimum_human_delay_seconds(content, f"editor:{step_id}")
            self.pv_editor_pending = {
                **pending_base,
                "mode": "time",
                "minimum": minimum,
            }
            await event.answer()
            await self._render_editor(
                event,
                "⏱ ALTERAR TEMPO\n\n"
                f"Atual: {int(row['median_delay_seconds'])} s\n"
                f"Mínimo humano para esta fala: {minimum} s.\n\n"
                "Envie a nova mediana em segundos. Zero não é permitido.",
                [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
            )
            return
        if action == "intent":
            current = await self._get_intent(step_id, block_key)
            self.pv_editor_pending = {**pending_base, "mode": "intent"}
            await event.answer()
            await self._render_editor(
                event,
                "🎯 ALTERAR INTENÇÃO DA FALA\n\n"
                f"Atual:\n{current}\n\n"
                "Descreva em uma frase o papel desta fala dentro da fase.",
                [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
            )
            return
        if action == "add":
            self.pv_editor_pending = {**pending_base, "mode": "add_text"}
            await event.answer()
            await self._render_editor(
                event,
                "➕ ADICIONAR À SEQUÊNCIA\n\n"
                "Envie a nova fala. Ela ficará DEPOIS da fala selecionada; "
                "não cria uma nova variação.",
                [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
            )
            return

    async def _delete_step(
        self,
        event,
        step_id: int,
        *,
        sequence_root: int | None = None,
    ) -> None:
        row = await self._message_store().get(step_id)
        block_key = str(row["block_key"]) if row is not None else None
        self.pv_editor_pending = None
        deleted = await self._message_store().delete(step_id)
        await event.answer(
            "Fala excluída." if deleted else "A fala já não existia.",
            alert=True,
        )
        if sequence_root is not None and sequence_root != step_id:
            root = await self._message_store().get(sequence_root)
            if root is not None:
                await self.show_sequence(event, sequence_root, sequence_root)
                return
        if block_key:
            await self.show_phase(event, block_key)
        else:
            await self.show_phase_index(event, 0)

    async def _consume_editor_input(self, event) -> None:
        pending = dict(self.pv_editor_pending or {})
        mode = pending.get("mode")
        step_id = int(pending.get("step_id", 0))
        store = self._message_store()
        raw = event.raw_text or ""

        if mode == "confirm_delete":
            await self._render_editor(
                event,
                "Use os botões de confirmação ou Cancelar.",
                [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
            )
            return

        if mode == "text":
            self.pv_editor_pending = None
            outcome = await store.set_content(step_id, raw)
            if outcome == "missing":
                await self.show_phase_index(event, 0)
                return
            await self._return_view(event, pending, step_id)
            return

        if mode == "intent":
            value = raw.strip()
            if not value:
                await self._render_editor(
                    event,
                    "A intenção precisa ter texto. Envie uma frase curta.",
                    [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
                )
                return
            await self._set_intent(step_id, value)
            self.pv_editor_pending = None
            await self._return_view(event, pending, step_id)
            return

        if mode == "time":
            value = raw.strip()
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
            self.pv_editor_pending = None
            await store.set_delay(step_id, seconds)
            await self._return_view(event, pending, step_id)
            return

        if mode == "add_text":
            value = raw.strip()
            if not value:
                await self._render_editor(
                    event,
                    "A nova fala precisa ter conteúdo.",
                    [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
                )
                return
            minimum = minimum_human_delay_seconds(value, f"editor:new:{step_id}")
            self.pv_editor_pending = {
                **pending,
                "mode": "add_time",
                "content": value,
                "minimum": minimum,
            }
            await self._render_editor(
                event,
                "⏱ TEMPO DA NOVA FALA\n\n"
                f"Mínimo humano para este texto: {minimum} s.\n"
                "Envie a mediana em segundos. Zero não é permitido.",
                [[Button.inline("❌ Cancelar", b"pvm:cancel")]],
            )
            return

        if mode == "add_time":
            value = raw.strip()
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
            content = str(pending.get("content") or "").strip()
            created = await store.add_after(step_id, content, seconds)
            self.pv_editor_pending = None
            target_id = int(created["id"]) if created else step_id
            root_id = pending.get("sequence_root") or step_id
            await self.show_sequence(event, int(root_id), target_id)
            return

        self.pv_editor_pending = None
        await self.show_phase_index(event, 0)

    async def _return_view(
        self,
        event,
        pending: dict,
        preferred_step_id: int | None = None,
    ) -> None:
        sequence_root = pending.get("sequence_root")
        if sequence_root:
            root = await self._message_store().get(int(sequence_root))
            if root is not None:
                await self.show_sequence(
                    event,
                    int(sequence_root),
                    preferred_step_id or int(sequence_root),
                )
                return
        block_key = pending.get("block_key")
        if block_key:
            await self.show_phase(event, str(block_key), preferred_step_id)
            return
        await self.show_phase_index(event, 0)

    async def _all_rows_for_block(self, block_key: str):
        rows = await self._message_store().list_all()
        return [row for row in rows if str(row["block_key"]) == block_key]

    @staticmethod
    def _phase_choices(rows):
        roots = [row for row in rows if bool(row["variant_root"])]
        return roots if roots else rows

    @staticmethod
    def _display_content(row) -> str:
        content = " ".join(str(row["content"] or "").split())
        media_kind = ""
        try:
            media_kind = str(row["media_kind"] or "").strip().lower()
        except (KeyError, TypeError):
            media_kind = ""
        media_label = {
            "photo": "📷 Foto",
            "video": "🎬 Vídeo",
            "gif": "🎞 GIF",
        }.get(media_kind, "")
        if media_label and content:
            return f"{media_label} · {content}"
        if media_label:
            return media_label
        return content

    async def show_phase_index(self, event, page: int = 0) -> None:
        rows = await self._message_store().list_all()
        present = {str(row["block_key"]) for row in rows}
        blocks = [block for block in BLOCK_ORDER if block in present]
        pages = max(1, math.ceil(len(blocks) / PHASE_PAGE_SIZE))
        page = max(0, min(int(page), pages - 1))
        current = blocks[page * PHASE_PAGE_SIZE : (page + 1) * PHASE_PAGE_SIZE]

        buttons = []
        for block in current:
            block_rows = [row for row in rows if str(row["block_key"]) == block]
            roots = [row for row in block_rows if bool(row["variant_root"])]
            if roots:
                suffix = f"{len(roots)} variações"
            elif len(block_rows) > 1:
                suffix = f"{len(block_rows)} falas"
            else:
                suffix = "1 fala"
            buttons.append(
                [
                    Button.inline(
                        f"{DISPLAY_BLOCK_LABELS.get(block, block)} · {suffix}",
                        f"pvm:b:{block}".encode(),
                    )
                ]
            )

        nav = []
        if page > 0:
            nav.append(Button.inline("⬅️", f"pvm:ph:{page-1}".encode()))
        nav.append(Button.inline(f"{page+1}/{pages}", f"pvm:ph:{page}".encode()))
        if page + 1 < pages:
            nav.append(Button.inline("➡️", f"pvm:ph:{page+1}".encode()))
        buttons.append(nav)
        buttons.append([Button.inline("↩️ Atendimento PV", b"menu:pv")])

        await self._render_editor(
            event,
            "💬 ATENDIMENTO PV — FALAS\n\n"
            "Escolha uma fase da conversa.\n"
            "Dentro dela você seleciona uma fala; os controles aparecem uma única vez.",
            buttons,
        )

    async def show_phase(
        self,
        event,
        block_key: str,
        selected_id: int | None = None,
    ) -> None:
        rows = await self._all_rows_for_block(block_key)
        if not rows:
            await self.show_phase_index(event, 0)
            return
        choices = self._phase_choices(rows)
        selected = next(
            (row for row in choices if int(row["id"]) == int(selected_id or 0)),
            choices[0],
        )
        selected_id = int(selected["id"])
        phase_intent, context = PHASE_CONTEXT.get(
            block_key,
            ("Cumprir a função desta fase da jornada.", "Mantém a continuidade do fluxo."),
        )
        roots = [row for row in rows if bool(row["variant_root"])]
        kind_line = (
            f"🗣 Variações ativas: {len(roots)}"
            if roots
            else f"🗣 Falas nesta fase: {len(choices)}"
        )

        lines = [
            f"💬 {DISPLAY_BLOCK_LABELS.get(block_key, block_key).upper()}",
            "",
            "🎯 Intenção da fase",
            phase_intent,
            "",
            "📍 Contexto",
            context,
            "",
            kind_line,
            "",
            "Escolha uma fala:",
        ]
        for index, row in enumerate(choices, start=1):
            content = self._display_content(row)
            if len(content) > 105:
                content = content[:102] + "..."
            marker = "✅" if int(row["id"]) == selected_id else "○"
            lines.append(f"{marker} {index}. {content}")

        selected_index = next(
            index for index, row in enumerate(choices, start=1) if int(row["id"]) == selected_id
        )
        intent = await self._get_intent(selected_id, block_key)
        sequence_rows = await self._sequence_rows(selected)
        selected_position = next(
            (index for index, row in enumerate(sequence_rows) if int(row["id"]) == selected_id),
            0,
        )
        after_count = max(0, len(sequence_rows) - selected_position - 1)
        sequence_label = (
            "nenhuma fala depois desta"
            if after_count == 0
            else f"{after_count} fala(s) depois desta"
        )
        lines += [
            "",
            "────────────",
            f"✅ FALA SELECIONADA — {selected_index} de {len(choices)}",
            f"“{self._display_content(selected)}”",
            "",
            "🎯 Intenção desta fala",
            intent,
            "",
            f"⏱ Tempo: {int(selected['median_delay_seconds'])} s",
            f"⚙️ Sequência: {sequence_label}",
        ]

        select_buttons = []
        current_row = []
        for index, row in enumerate(choices, start=1):
            label = f"✅ {index}" if int(row["id"]) == selected_id else str(index)
            current_row.append(Button.inline(label, f"pvm:s:{int(row['id'])}".encode()))
            if len(current_row) == 5:
                select_buttons.append(current_row)
                current_row = []
        if current_row:
            select_buttons.append(current_row)

        buttons = select_buttons + [
            [
                Button.inline("✏️ Texto", f"pvm:text:{selected_id}".encode()),
                Button.inline("⏱ Tempo", f"pvm:time:{selected_id}".encode()),
            ],
            [
                Button.inline("🎯 Intenção", f"pvm:intent:{selected_id}".encode()),
                Button.inline("⚙️ Sequência", f"pvm:seq:{selected_id}".encode()),
            ],
            [Button.inline("🗑 Excluir fala", f"pvm:delete:{selected_id}".encode())],
            [Button.inline("↩️ Fases", b"pvm:home")],
        ]
        await self._render_editor(event, "\n".join(lines)[:3900], buttons)

    async def _sequence_rows(self, row):
        block_key = str(row["block_key"])
        branch_key = row["branch_key"]
        return await self._message_store().block_rows(block_key, branch_key)

    async def show_sequence(
        self,
        event,
        root_id: int,
        selected_id: int | None = None,
    ) -> None:
        root = await self._message_store().get(root_id)
        if root is None:
            await self.show_phase_index(event, 0)
            return
        rows = list(await self._sequence_rows(root))
        if not rows:
            await self.show_phase(event, str(root["block_key"]))
            return
        selected = next(
            (row for row in rows if int(row["id"]) == int(selected_id or 0)),
            rows[0],
        )
        selected_id = int(selected["id"])
        block_key = str(root["block_key"])
        phase_intent, context = PHASE_CONTEXT.get(
            block_key,
            ("Cumprir a função desta fase da jornada.", "Mantém a continuidade do fluxo."),
        )
        intent = await self._get_intent(selected_id, block_key)

        lines = [
            f"⚙️ SEQUÊNCIA — {DISPLAY_BLOCK_LABELS.get(block_key, block_key).upper()}",
            "",
            f"📍 Contexto: {context}",
            "",
            "Ordem desta sequência:",
        ]
        for index, row in enumerate(rows, start=1):
            marker = "✅" if int(row["id"]) == selected_id else "○"
            content = self._display_content(row)
            if len(content) > 100:
                content = content[:97] + "..."
            lines.append(f"{marker} {index}. {content}")
        lines += [
            "",
            "────────────",
            f"✅ FALA SELECIONADA — {next(i for i, r in enumerate(rows, 1) if int(r['id']) == selected_id)} de {len(rows)}",
            f"“{self._display_content(selected)}”",
            "",
            "🎯 Intenção desta fala",
            intent,
            "",
            f"⏱ Tempo: {int(selected['median_delay_seconds'])} s",
            "",
            f"🎯 Intenção da fase: {phase_intent}",
        ]

        select_buttons = []
        current_row = []
        for index, row in enumerate(rows, start=1):
            label = f"✅ {index}" if int(row["id"]) == selected_id else str(index)
            current_row.append(
                Button.inline(label, f"pvm:q:{root_id}:{int(row['id'])}".encode())
            )
            if len(current_row) == 5:
                select_buttons.append(current_row)
                current_row = []
        if current_row:
            select_buttons.append(current_row)

        buttons = select_buttons + [
            [
                Button.inline(
                    "✏️ Texto",
                    f"pvmx:text:{root_id}:{selected_id}".encode(),
                ),
                Button.inline(
                    "⏱ Tempo",
                    f"pvmx:time:{root_id}:{selected_id}".encode(),
                ),
            ],
            [
                Button.inline(
                    "🎯 Intenção",
                    f"pvmx:intent:{root_id}:{selected_id}".encode(),
                ),
                Button.inline(
                    "➕ Depois",
                    f"pvmx:add:{root_id}:{selected_id}".encode(),
                ),
            ],
            [
                Button.inline(
                    "🗑 Excluir fala",
                    f"pvmx:delete:{root_id}:{selected_id}".encode(),
                )
            ],
            [Button.inline("↩️ Voltar à fase", f"pvm:b:{block_key}".encode())],
        ]
        await self._render_editor(event, "\n".join(lines)[:3900], buttons)

    async def show_pv_message_editor(self, event, page: int = 0) -> None:
        """Compatibility entrypoint: the editor now opens on the phase index."""
        await self.show_phase_index(event, page)
