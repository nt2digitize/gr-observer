"""Compact operator-first navigation layered over the existing control panel.

This mixin changes only presentation/navigation. Existing text commands, module
IDs, callbacks, state machines, one Telegram USER session, one Outbox and one
Writer remain untouched.
"""

from __future__ import annotations

from telethon import Button, events


class CleanMenuPanelMixin:
    """Group the operational surface into short, contextual button menus."""

    def _module_state(self, module_id: str) -> tuple[str, str, bool]:
        item = self.app.registry.get(module_id)
        enabled = bool(item.enabled)
        return (
            "🟢 LIGADO" if enabled else "🔴 DESLIGADO",
            str(item.reason or ""),
            enabled,
        )

    async def _render_menu(self, event, text: str, buttons) -> None:
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
        else:
            await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)

    @staticmethod
    def _toggle_button(module_id: str, enabled: bool, label: str):
        direction = "off" if enabled else "on"
        verb = "⏸ Desligar" if enabled else "▶️ Ligar"
        return Button.inline(
            f"{verb} {label}",
            f"menu:toggle:{module_id}:{direction}".encode(),
        )

    async def show_dashboard(self, event) -> None:
        radar_state, _, _ = self._module_state("radar")
        pv_state, _, _ = self._module_state("pv_reply")
        group_state, _, _ = self._module_state("group_reply")
        text = (
            "🔎 GR OBSERVER — PAINEL\n\n"
            f"📡 Radar: {radar_state}\n"
            f"💬 Atendimento PV: {pv_state}\n"
            f"👥 Atendimento de Grupos: {group_state}\n\n"
            "Escolha uma área. Os comandos de texto continuam funcionando como atalhos."
        )
        buttons = [
            [
                Button.inline("📡 Radar", b"menu:radar"),
                Button.inline("💬 Atendimento PV", b"menu:pv"),
            ],
            [
                Button.inline("👥 Grupos", b"menu:groups"),
                Button.inline("🗂 Inventário", b"menu:inventory"),
            ],
            [
                Button.inline("🧭 Organização", b"menu:organizer"),
                Button.inline("⚙️ Sistema", b"menu:system"),
            ],
            [Button.inline("📊 Status completo", b"menu:status")],
        ]
        await self._render_menu(event, text, buttons)

    async def show_radar_menu(self, event) -> None:
        state, reason, enabled = self._module_state("radar")
        text = (
            "📡 RADAR\n\n"
            f"Estado: {state}\n"
            f"Motivo: {reason}\n\n"
            "Observação, inventário e classificação de grupos/canais."
        )
        buttons = [
            [self._toggle_button("radar", enabled, "Radar")],
            [
                Button.inline("📢 Canais", b"channels"),
                Button.inline("👥 Grupos", b"groups"),
            ],
            [
                Button.inline("✅ Postáveis", b"postable"),
                Button.inline("⚠️ Incertos", b"uncertain"),
            ],
            [
                Button.inline("🤖 Bots", b"bots"),
                Button.inline("🔗 Links", b"links"),
            ],
            [Button.inline("↩️ Início", b"home")],
        ]
        await self._render_menu(event, text, buttons)

    async def show_pv_menu(self, event) -> None:
        state, reason, enabled = self._module_state("pv_reply")
        text = (
            "💬 ATENDIMENTO PV\n\n"
            f"Estado: {state}\n"
            f"Motivo: {reason}\n\n"
            "Falas, tempos, destino, fotos e campanhas do atendimento privado."
        )
        buttons = [
            [self._toggle_button("pv_reply", enabled, "Atendimento")],
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

    async def show_groups_menu(self, event) -> None:
        state, reason, enabled = self._module_state("group_reply")
        text = (
            "👥 ATENDIMENTO DE GRUPOS\n\n"
            f"Estado: {state}\n"
            f"Motivo: {reason}\n\n"
            "Respostas reativas, ADD, proteção e repostagens autorizadas."
        )
        buttons = [
            [self._toggle_button("group_reply", enabled, "Grupos")],
            [
                Button.inline("📋 Configuração", b"menu:groups:preview"),
                Button.inline("🧭 Organizador", b"organizer"),
            ],
            [
                Button.inline("🟢 Postagens ativas", b"list:posting_active:0"),
                Button.inline("🟡 Falta publicar", b"list:joined_pending:0"),
            ],
            [
                Button.inline("⏰ Fechados", b"list:joined_closed:0"),
                Button.inline("👥 Para conhecer", b"list:candidate_groups:0"),
            ],
            [Button.inline("↩️ Início", b"home")],
        ]
        await self._render_menu(event, text, buttons)

    async def show_inventory_menu(self, event) -> None:
        counts = await self.app.storage.counts_for_dashboard()
        bots = await self.app.pool.fetchval(
            "SELECT COUNT(DISTINCT bot_id) FROM observed_bots"
        )
        links = await self.app.pool.fetchval(
            "SELECT COUNT(*) FROM discovered_links WHERE status='pending'"
        )
        text = (
            "🗂 INVENTÁRIO\n\n"
            "Consulta do que o Radar já conhece. Nenhuma ação de escrita é executada aqui."
        )
        buttons = [
            [
                Button.inline(f"📢 Canais ({counts['channels']})", b"channels"),
                Button.inline(f"👥 Grupos ({counts['groups']})", b"groups"),
            ],
            [
                Button.inline(f"✅ Postáveis ({counts['postable']})", b"postable"),
                Button.inline(f"⚠️ Incertos ({counts['uncertain']})", b"uncertain"),
            ],
            [
                Button.inline(f"🤖 Bots ({bots})", b"bots"),
                Button.inline(f"🔗 Links ({links})", b"links"),
            ],
            [
                Button.inline("👤 Origens PV", b"origins"),
                Button.inline("📝 Rascunhos", b"drafts"),
            ],
            [Button.inline("↩️ Início", b"home")],
        ]
        await self._render_menu(event, text, buttons)

    async def show_system_menu(self, event) -> None:
        _, _, botson_enabled = self._module_state("botson")
        text = (
            "⚙️ SISTEMA\n\n"
            "Estado geral, módulos e homologação.\n"
            "A operação diária deve ficar nos menus Radar, PV e Grupos."
        )
        buttons = [
            [
                Button.inline("📊 Status completo", b"menu:status"),
                Button.inline("🧩 Módulos", b"functions"),
            ],
            [self._toggle_button("botson", botson_enabled, "BOTSON")],
            [Button.inline("🧪 Executar teste BOTSON", b"botson:run")],
            [Button.inline("⌨️ Atalhos de texto", b"menu:commands")],
            [Button.inline("↩️ Início", b"home")],
        ]
        await self._render_menu(event, text, buttons)

    async def show_status_menu(self, event) -> None:
        await self._render_menu(
            event,
            "📊 STATUS COMPLETO\n\n" + self.app.status_text(),
            [[Button.inline("↩️ Início", b"home")]],
        )

    async def show_group_preview_menu(self, event) -> None:
        text = self.app.registry.get("group_reply").implementation.preview()
        await self._render_menu(
            event,
            "📋 CONFIGURAÇÃO — GRUPOS\n\n" + text,
            [[Button.inline("↩️ Grupos", b"menu:groups")]],
        )

    async def show_commands_menu(self, event) -> None:
        from .catalog import COMMANDS

        lines = [
            "⌨️ ATALHOS DE TEXTO",
            "",
            "Você não precisa decorar estes comandos; os botões fazem as mesmas ações.",
            "",
        ]
        for _, spec in sorted(COMMANDS.items(), key=lambda entry: entry[1]["order"]):
            if "panel" not in spec["surfaces"]:
                continue
            preferred = next(
                (value for value in spec["triggers"] if value.startswith("/")),
                spec["triggers"][0],
            )
            lines.append(f"• {preferred} — {spec['help']}")
        await self._render_menu(
            event,
            "\n".join(lines)[:3800],
            [[Button.inline("↩️ Sistema", b"menu:system")]],
        )

    async def show_functions(self, event) -> None:
        """Compact module manager; legacy /funcoes remains a valid shortcut."""
        lines = ["🧩 MÓDULOS", ""]
        buttons = []
        for item in self.app.registry.ordered():
            icon = "🟢" if item.enabled else "🔴"
            lines.append(f"{icon} {item.spec['label']}")
            buttons.append(
                [self._toggle_button(item.module_id, bool(item.enabled), item.spec["label"])]
            )
        buttons.append([Button.inline("↩️ Sistema", b"menu:system")])
        await self._render_menu(event, "\n".join(lines), buttons)

    async def on_callback(self, event) -> None:
        if not self.app.is_admin(event):
            return await super().on_callback(event)

        data = event.data.decode("utf-8", errors="replace")
        routes = {
            "menu:home": self.show_dashboard,
            "menu:radar": self.show_radar_menu,
            "menu:pv": self.show_pv_menu,
            "menu:groups": self.show_groups_menu,
            "menu:inventory": self.show_inventory_menu,
            "menu:organizer": self.show_organizer,
            "menu:system": self.show_system_menu,
            "menu:status": self.show_status_menu,
            "menu:groups:preview": self.show_group_preview_menu,
            "menu:commands": self.show_commands_menu,
        }
        handler = routes.get(data)
        if handler is not None:
            await event.answer()
            await handler(event)
            return

        if data.startswith("menu:toggle:"):
            parts = data.split(":")
            if len(parts) == 4:
                _, _, module_id, direction = parts
                if module_id in {"radar", "pv_reply", "group_reply", "botson"} and direction in {"on", "off"}:
                    result = await self.app.set_module_enabled(module_id, direction == "on")
                    await event.answer(result[:200], alert=True)
                    target = {
                        "radar": self.show_radar_menu,
                        "pv_reply": self.show_pv_menu,
                        "group_reply": self.show_groups_menu,
                        "botson": self.show_system_menu,
                    }[module_id]
                    await target(event)
                    return

        await super().on_callback(event)
