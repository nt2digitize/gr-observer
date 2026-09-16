"""Explicit integration adapter for the Group Attendance rib.

This module contains no import-time mutation. The composition root calls
``register_group_reply`` explicitly and selects ``GroupControlPanel`` explicitly,
so the module catalog remains the single source of IDs/order and Observer.setup
remains owned by the application spine.
"""

from __future__ import annotations

import re
import time

from telethon import Button, types

from .catalog import match_command
from .clean_menu_panel import CleanMenuPanelMixin
from .modules.group_reply_contacts import GroupReplyWithContacts
from .panel import ControlPanel as BaseControlPanel
from .pv_editor_policy import PvMessageEditorPanel
from .pv_suppression import (
    list_suppressed,
    recent_pv_users,
    resolve_pv_user,
    suppress_pv_user,
)


def register_group_reply(application) -> None:
    """Attach the Group Attendance rib to the existing central registry."""
    application.registry.register(
        "group_reply",
        GroupReplyWithContacts(application.storage, application.settings),
    )


class GroupControlPanel(
    CleanMenuPanelMixin,
    PvMessageEditorPanel,
    BaseControlPanel,
):
    """Operator panel with compact menus, group controls and PV copy editor."""

    _PV_STOP_INPUT_TIMEOUT_SECONDS = 180

    async def show_pv_menu(self, event) -> None:
        """Organize the PV operator surface without changing any runtime flow."""
        state, reason, enabled = self._module_state("pv_reply")
        text = (
            "💬 ATENDIMENTO PV\n\n"
            f"Estado: {state}\n"
            f"Motivo: {reason}\n\n"
            "Organização do painel por responsabilidade. Os fluxos, estados, tempos, "
            "Outbox e ações existentes continuam os mesmos."
        )
        buttons = [
            [self._toggle_button("pv_reply", enabled, "Atendimento")],
            [
                Button.inline("👋 Entrada", b"pvmenu:entry"),
                Button.inline("📸 Duas telas / Homenagem", b"pvmenu:two_screens"),
            ],
            [
                Button.inline("⚡ Eventos", b"pvmenu:events"),
                Button.inline("♻️ Remarketing", b"pvmenu:remarketing"),
            ],
            [Button.inline("💭 Conversa livre", b"pvmenu:conversation")],
            [Button.inline("⏸ Pausar chat de uma pessoa", b"pvstop:picker")],
            [Button.inline("⚙️ Configuração", b"pvmenu:config")],
            [Button.inline("↩️ Início", b"home")],
        ]
        await self._render_menu(event, text, buttons)

    async def _show_pv_section_menu(self, event, section: str) -> None:
        """Route new visual sections to already-homologated operator actions."""
        sections = {
            "entry": (
                "👋 ENTRADA",
                "Saudação, primeiro acesso/link e pós-link, usando exatamente os blocos atuais.",
                [
                    [Button.inline("👋 Saudação", b"pvm:b:greeting")],
                    [Button.inline("🔗 Primeiro acesso / link", b"pvm:b:link")],
                    [Button.inline("💬 Pós-link", b"pvm:b:followup")],
                ],
            ),
            "two_screens": (
                "📸 DUAS TELAS / HOMENAGEM",
                "Convite, escolha, mídias e resposta à homenagem no mesmo ramo. "
                "Depois de uma mídia Duas Telas realmente entregue, foto/vídeo/GIF recebido no PV "
                "é tratado como homenagem e não como nova escolha ambígua.",
                [
                    [Button.inline("💬 Convite", b"pvm:b:two_screens_prompt")],
                    [Button.inline("🎯 Escolha", b"pvm:b:two_screens_preference")],
                    [Button.inline("📷 Mídias", b"pv:two_screens")],
                    [Button.inline("💌 Resposta à homenagem", b"pvm:b:two_screens_followup")],
                    [Button.inline("🔁 Repetir escolha", b"pvm:b:two_screens_retry_preference")],
                ],
            ),
            "events": (
                "⚡ EVENTOS",
                "A superfície já é organizada como evento; nesta camada o único tipo executável continua sendo live, "
                "reutilizando o backend homologado sem criar outro motor de campanha.",
                [
                    [Button.inline("🔴 Criar evento / live", b"live:new")],
                    [Button.inline("👂 Consentimento", b"pvm:b:live_optin")],
                    [Button.inline("📣 Convite", b"pvm:b:live_invite")],
                    [Button.inline("♻️ Remarketing do evento", b"pvm:b:live_remarketing")],
                    [Button.inline("🔗 Mensagem + destino", b"pvm:b:live_link")],
                ],
            ),
            "remarketing": (
                "♻️ REMARKETING",
                "Os ciclos continuam no scheduler e na Outbox atuais. Aqui só ficam organizadas as cadências que já existem; "
                "nenhum agendamento novo é criado pelo menu.",
                [
                    [Button.inline("☀️ Diário / progressivo", b"pvm:b:followup")],
                    [Button.inline("🔗 Reenvio de destino", b"pvm:b:reminder_link")],
                    [Button.inline("📅 Semanal", b"pvm:b:weekly")],
                    [Button.inline("🔴 Evento", b"pvm:b:live_remarketing")],
                ],
            ),
            "conversation": (
                "💭 CONVERSA LIVRE",
                "Área reservada para Contextos e Memória, fatos e respostas observadas. Nesta camada nada novo responde automaticamente.",
                [
                    [Button.inline("👤 Origens PV", b"origins")],
                    [Button.inline("📝 Rascunhos", b"drafts")],
                ],
            ),
            "config": (
                "⚙️ CONFIGURAÇÃO — PV",
                "Configuração existente do Atendimento PV, sem nova variável, serviço ou worker.",
                [[Button.inline("✏️ Falas, tempos + destino", b"command:pv_preview")]],
            ),
        }
        selected = sections.get(section)
        if selected is None:
            await self.show_pv_menu(event)
            return
        title, description, buttons = selected
        buttons = list(buttons) + [[Button.inline("↩️ Atendimento PV", b"menu:pv")]]
        await self._render_menu(event, f"{title}\n\n{description}", buttons)

    @staticmethod
    def _pv_stop_label(row) -> str:
        user_id = int(row["user_id"])
        username = str(row["username"] or "").strip().lstrip("@")
        display_name = str(row["display_name"] or "").strip()
        if display_name and username:
            identity = f"{display_name} @{username}"
        elif display_name:
            identity = display_name
        elif username:
            identity = f"@{username}"
        else:
            identity = f"ID {user_id}"
        return f"⏸ {identity} · {str(user_id)[-4:]}"[:60]

    @staticmethod
    def _forwarded_user_id(event) -> int | None:
        """Return a trustworthy Telegram user id from a forwarded message."""
        message = getattr(event, "message", None)
        header = getattr(message, "fwd_from", None)
        peer = getattr(header, "from_id", None)
        if not isinstance(peer, types.PeerUser):
            return None
        user_id = int(getattr(peer, "user_id", 0) or 0)
        return user_id if user_id > 0 else None

    def _arm_pv_stop_input(self, mode: str) -> None:
        self._pv_stop_input_mode = mode
        self._pv_stop_input_until = time.monotonic() + self._PV_STOP_INPUT_TIMEOUT_SECONDS

    def _consume_pv_stop_input_mode(self) -> str | None:
        mode = str(getattr(self, "_pv_stop_input_mode", "") or "")
        until = float(getattr(self, "_pv_stop_input_until", 0.0) or 0.0)
        if not mode or time.monotonic() > until:
            self._pv_stop_input_mode = ""
            self._pv_stop_input_until = 0.0
            return None
        return mode

    def _clear_pv_stop_input(self) -> None:
        self._pv_stop_input_mode = ""
        self._pv_stop_input_until = 0.0

    async def _pv_stop_identity(self, user_id: int) -> tuple[str, str | None]:
        """Return a human label for confirmation without exposing internal ids."""
        row = await self.app.pool.fetchrow(
            """SELECT username,display_name FROM pv_reply_contacts WHERE user_id=$1
               UNION ALL
               SELECT username,display_name FROM contact_ledger WHERE user_id=$1
               LIMIT 1""",
            int(user_id),
        )
        if row is None:
            return "Abrir contato", None
        username = str(row["username"] or "").strip().lstrip("@") or None
        display_name = str(row["display_name"] or "").strip()
        if display_name:
            label = display_name
        elif username:
            label = f"@{username}"
        else:
            label = "Abrir contato"
        return label[:50], username

    async def _show_pv_stop_confirmation(self, event, user_id: int) -> None:
        label, username = await self._pv_stop_identity(user_id)
        identity_line = f"Pessoa encontrada: {label}"
        if username and label != f"@{username}":
            identity_line += f"\n@{username}"
        text = (
            "⏸ CONFIRMAR PAUSA INDIVIDUAL\n\n"
            f"{identity_line}\n\n"
            "Toque no nome para conferir o contato certo antes de pausar.\n\n"
            "Isso pausa somente o chat automático desta pessoa. "
            "O contato continua salvo, o chat manual continua normal, não bloqueia no Telegram "
            "e nenhum outro chat é afetado."
        )
        buttons = [
            [Button.url(f"👤 {label}", f"tg://user?id={int(user_id)}")],
            [Button.inline("✅ Pausar este chat", f"pvstop:confirm:{int(user_id)}".encode())],
            [Button.inline("↩️ Voltar", b"pvstop:picker")],
        ]
        await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)

    async def _show_pv_stop_picker(self, event, *, edit: bool = False) -> None:
        rows = await recent_pv_users(self.app.pool, limit=12)
        text = (
            "⏸ PAUSAR CHAT AUTOMÁTICO DE UMA PESSOA\n\n"
            "Você pode escolher uma pessoa conhecida, buscar por @username/ID ou encaminhar uma mensagem dela. "
            "A pausa vale somente para aquele usuário e pode ser aplicada antes de ele entrar no funil.\n\n"
            "Se a origem do encaminhamento estiver oculta pelo Telegram, ninguém será pausado."
        )
        buttons = [
            [
                Button.inline("🔎 Buscar por @ ou ID", b"pvstop:search"),
                Button.inline("📩 Encaminhar mensagem", b"pvstop:forward"),
            ]
        ]
        buttons.extend(
            [
                [
                    Button.inline(
                        self._pv_stop_label(row),
                        f"pvstop:pick:{int(row['user_id'])}".encode(),
                    )
                ]
                for row in rows
            ]
        )
        buttons.append([Button.inline("↩️ Atendimento PV", b"menu:pv")])
        if edit:
            await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
        else:
            await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)

    async def _handle_pv_stop_pending_input(self, event, raw_text: str) -> bool:
        mode = self._consume_pv_stop_input_mode()
        if mode is None:
            return False

        if mode == "forward":
            message = getattr(event, "message", None)
            if getattr(message, "fwd_from", None) is None:
                return False
            self._clear_pv_stop_input()
            user_id = self._forwarded_user_id(event)
            if user_id is None:
                await event.respond(
                    "Não consegui identificar a pessoa nesse encaminhamento. "
                    "O Telegram ocultou a origem; ninguém foi pausado.",
                    buttons=[[Button.inline("↩️ Tentar novamente", b"pvstop:picker")]],
                    parse_mode=None,
                )
                return True
            await self._show_pv_stop_confirmation(event, user_id)
            return True

        if mode == "search" and raw_text:
            self._clear_pv_stop_input()
            user_id = await resolve_pv_user(self.app.pool, raw_text)
            if user_id is None:
                await event.respond(
                    "Não achei esse @username no histórico conhecido. "
                    "Use o ID numérico ou encaminhe uma mensagem da pessoa.",
                    buttons=[[Button.inline("📩 Encaminhar mensagem", b"pvstop:forward")]],
                    parse_mode=None,
                )
                return True
            await self._show_pv_stop_confirmation(event, user_id)
            return True

        return False

    async def _handle_pv_kill_switch(self, event, raw_text: str) -> bool:
        stop_match = re.fullmatch(
            r"(?:/parar_usuario|/stop_user|parar\s+usuario)(?:\s+(.+))?",
            raw_text,
            flags=re.I,
        )
        if stop_match:
            target = (stop_match.group(1) or "").strip()
            if not target:
                await self._show_pv_stop_picker(event)
                return True
            user_id = await resolve_pv_user(self.app.pool, target)
            if user_id is None:
                await event.respond(
                    "Não achei esse @username no histórico conhecido. Use o ID numérico ou encaminhe uma mensagem da pessoa.",
                    parse_mode=None,
                )
                return True
            await self._show_pv_stop_confirmation(event, user_id)
            return True

        if re.fullmatch(r"/(?:usuarios_parados|parados)", raw_text, flags=re.I):
            rows = await list_suppressed(self.app.pool)
            if not rows:
                await event.respond("Nenhum chat automático individual está pausado.", parse_mode=None)
                return True
            lines = ["⏸ CHATS AUTOMÁTICOS PAUSADOS", ""]
            for row in rows:
                username = row["username"] or "sem @username"
                if username != "sem @username" and not str(username).startswith("@"):
                    username = "@" + str(username)
                lines.append(f"• {row['user_id']} — {username}")
            await event.respond("\n".join(lines)[:3900], parse_mode=None)
            return True
        return False

    async def on_message(self, event) -> None:
        if not self.app.is_admin(event):
            return
        raw_text = (event.raw_text or "").strip()
        if await self._handle_pv_stop_pending_input(event, raw_text):
            return
        if await self._handle_pv_kill_switch(event, raw_text):
            return
        command_id = match_command(raw_text, "panel")
        if command_id == "group_reply.enable":
            await event.respond(
                await self.app.set_module_enabled("group_reply", True), parse_mode=None
            )
            return
        if command_id == "group_reply.disable":
            await event.respond(
                await self.app.set_module_enabled("group_reply", False), parse_mode=None
            )
            return
        if command_id == "group_reply.preview":
            await event.respond(
                self.app.registry.get("group_reply").implementation.preview(),
                parse_mode=None,
                link_preview=False,
            )
            return
        await super().on_message(event)

    async def on_callback(self, event) -> None:
        if not self.app.is_admin(event):
            await super().on_callback(event)
            return
        data = event.data.decode("utf-8", errors="replace")
        section_match = re.fullmatch(
            r"pvmenu:(entry|two_screens|events|remarketing|conversation|config)", data
        )
        if section_match:
            await event.answer()
            await self._show_pv_section_menu(event, section_match.group(1))
            return
        if data == "pvstop:picker":
            self._clear_pv_stop_input()
            await event.answer()
            await self._show_pv_stop_picker(event, edit=True)
            return
        if data == "pvstop:search":
            self._arm_pv_stop_input("search")
            await event.answer()
            await event.edit(
                "🔎 BUSCAR USUÁRIO\n\nEnvie agora o @username ou o ID numérico. "
                "Se não souber, volte e use Encaminhar mensagem.",
                buttons=[[Button.inline("↩️ Voltar", b"pvstop:picker")]],
                parse_mode=None,
                link_preview=False,
            )
            return
        if data == "pvstop:forward":
            self._arm_pv_stop_input("forward")
            await event.answer()
            await event.edit(
                "📩 IDENTIFICAR POR ENCAMINHAMENTO\n\nEncaminhe agora uma mensagem da pessoa. "
                "O Radar lê o user_id imediatamente; depois a mensagem original pode ser apagada sem desfazer a pausa.",
                buttons=[[Button.inline("↩️ Voltar", b"pvstop:picker")]],
                parse_mode=None,
                link_preview=False,
            )
            return
        pick_match = re.fullmatch(r"pvstop:pick:(\d+)", data)
        if pick_match:
            await event.answer()
            await self._show_pv_stop_confirmation(event, int(pick_match.group(1)))
            return
        confirm_match = re.fullmatch(r"pvstop:confirm:(\d+)", data)
        if confirm_match:
            user_id = int(confirm_match.group(1))
            neutralized = await suppress_pv_user(
                self.app.pool,
                user_id,
                suppressed_by=self.app.admin_id,
                reason="admin_panel",
            )
            await event.answer(
                f"Chat automático pausado. {neutralized} ação(ões) pendente(s) neutralizada(s).",
                alert=True,
            )
            await self._show_pv_stop_picker(event, edit=True)
            return
        if data in {"module:group_reply:on", "module:group_reply:off"}:
            enabled = data.endswith(":on")
            result = await self.app.set_module_enabled("group_reply", enabled)
            await event.answer(result[:200], alert=True)
            await self.show_functions(event)
            return
        await super().on_callback(event)
