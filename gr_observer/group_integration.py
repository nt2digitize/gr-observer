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
        """Expose the existing per-user kill switch in the daily PV surface."""
        state, reason, enabled = self._module_state("pv_reply")
        text = (
            "💬 ATENDIMENTO PV\n\n"
            f"Estado: {state}\n"
            f"Motivo: {reason}\n\n"
            "Falas, tempos, destino, fotos e campanhas do atendimento privado.\n"
            "O controle individual abaixo pausa somente o chat automático da pessoa escolhida; "
            "não bloqueia no Telegram, não remove o contato da agenda e não afeta outros chats."
        )
        buttons = [
            [self._toggle_button("pv_reply", enabled, "Atendimento")],
            [Button.inline("⏸ Pausar chat de uma pessoa", b"pvstop:picker")],
            [Button.inline("✏️ Falas, tempos + destino", b"command:pv_preview")],
            [
                Button.inline("📷 Fotos duas telas", b"pv:two_screens"),
                Button.inline("🔴 Nova live", b"live:new"),
            ],
            [
                Button.inline("👤 Origens PV", b"origins"),
                Button.inline("📝 Rascunhos", b"drafts"),
            ],
            [Button.inline("↩️ Início", b"home")],
        ]
        await self._render_menu(event, text, buttons)

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
            "O contato continua salvo, o chat manual continua normal e nenhum outro chat é afetado."
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
