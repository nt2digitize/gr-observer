"""Explicit integration adapter for the Group Attendance rib.

This module contains no import-time mutation. The composition root calls
``register_group_reply`` explicitly and selects ``GroupControlPanel`` explicitly,
so the module catalog remains the single source of IDs/order and Observer.setup
remains owned by the application spine.
"""

from __future__ import annotations

import re

from telethon import Button

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

    async def show_pv_menu(self, event) -> None:
        """Expose the existing per-user kill switch in the daily PV surface."""
        state, reason, enabled = self._module_state("pv_reply")
        text = (
            "💬 ATENDIMENTO PV\n\n"
            f"Estado: {state}\n"
            f"Motivo: {reason}\n\n"
            "Falas, tempos, destino, fotos e campanhas do atendimento privado.\n"
            "O controle individual abaixo para somente a automação da pessoa; "
            "não bloqueia no Telegram e não remove o contato da agenda."
        )
        buttons = [
            [self._toggle_button("pv_reply", enabled, "Atendimento")],
            [Button.inline("⏸ Tirar pessoa do funil", b"pvstop:picker")],
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
        return f"⛔ {identity} · {str(user_id)[-4:]}"[:60]

    async def _show_pv_stop_picker(self, event, *, edit: bool = False) -> None:
        rows = await recent_pv_users(self.app.pool, limit=12)
        if not rows:
            text = (
                "⛔ PARAR AUTOMAÇÃO DE UMA PESSOA\n\n"
                "Não há contatos recentes disponíveis para selecionar. "
                "Você ainda pode usar /parar_usuario <ID ou @username>.\n\n"
                "Isso não bloqueia a pessoa no Telegram e não remove o contato da agenda."
            )
            buttons = [[Button.inline("↩️ Atendimento PV", b"menu:pv")]]
        else:
            text = (
                "⛔ PARAR AUTOMAÇÃO DE UMA PESSOA\n\n"
                "Mais recentes primeiro. Toque somente na pessoa que deve sair do funil. "
                "O número final ajuda a diferenciar nomes iguais.\n\n"
                "O contato continua salvo e o chat manual continua normal."
            )
            buttons = [
                [
                    Button.inline(
                        self._pv_stop_label(row),
                        f"pvstop:{int(row['user_id'])}".encode(),
                    )
                ]
                for row in rows
            ]
            buttons.append([Button.inline("↩️ Atendimento PV", b"menu:pv")])
        if edit:
            await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
        else:
            await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)

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
                    "Não achei esse usuário no histórico PV. Envie /parar_usuario sem parâmetro para escolher na lista.",
                    parse_mode=None,
                )
                return True
            neutralized = await suppress_pv_user(
                self.app.pool,
                user_id,
                suppressed_by=self.app.admin_id,
                reason="admin_panel",
            )
            await event.respond(
                "⛔ USUÁRIO PARADO\n\n"
                f"Telegram user_id: {user_id}\n"
                f"Ações PV pendentes neutralizadas: {neutralized}\n\n"
                "Novas mensagens desse usuário não criam mais jornada automática. "
                "Uma RPC que já estivesse em voo no exato instante do bloqueio pode terminar; "
                "o restante da fila fica cortado. O contato e o chat manual permanecem intactos.",
                parse_mode=None,
            )
            return True

        if re.fullmatch(r"/(?:usuarios_parados|parados)", raw_text, flags=re.I):
            rows = await list_suppressed(self.app.pool)
            if not rows:
                await event.respond("Nenhum usuário está parado pelo kill switch.", parse_mode=None)
                return True
            lines = ["⛔ USUÁRIOS PARADOS", ""]
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
            await event.answer()
            await self._show_pv_stop_picker(event, edit=True)
            return
        stop_pick = re.fullmatch(r"pvstop:(\d+)", data)
        if stop_pick:
            user_id = int(stop_pick.group(1))
            neutralized = await suppress_pv_user(
                self.app.pool,
                user_id,
                suppressed_by=self.app.admin_id,
                reason="admin_picker",
            )
            await event.answer(
                f"Usuário parado. {neutralized} ação(ões) pendente(s) neutralizada(s).",
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