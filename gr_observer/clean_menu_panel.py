"""Compact operator-first navigation focused on open chat groups.

The operator surface intentionally hides channels, raw links and bot inventory from
daily work. Existing collectors, text commands, module IDs, one Telegram USER
session, one Outbox and one Writer remain untouched underneath.
"""

from __future__ import annotations

import re

from telethon import Button, errors, events


class CleanMenuPanelMixin:
    """Keep the daily UI focused on PV and writable chat groups."""

    GROUP_PAGE_SIZE = 8

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
            try:
                await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
            except errors.MessageNotModifiedError:
                return
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

    async def _group_counts(self) -> dict[str, int]:
        row = await self.app.pool.fetchrow(
            """SELECT
               COUNT(*) FILTER (
                 WHERE c.kind='group' AND c.membership_status='joined'
                   AND c.disposition='active' AND c.can_text IS TRUE
                   AND EXISTS(SELECT 1 FROM group_repost_state r WHERE r.chat_id=c.chat_id)
               ) AS active,
               COUNT(*) FILTER (
                 WHERE c.kind='group' AND c.membership_status='joined'
                   AND c.disposition='active' AND c.can_text IS NOT TRUE
                   AND EXISTS(SELECT 1 FROM group_repost_state r WHERE r.chat_id=c.chat_id)
               ) AS review,
               COUNT(*) FILTER (
                 WHERE c.kind='group' AND c.membership_status='joined'
                   AND c.disposition='active' AND c.can_text IS TRUE
                   AND NOT EXISTS(SELECT 1 FROM group_repost_state r WHERE r.chat_id=c.chat_id)
               ) AS ready
               FROM chats c"""
        )
        candidates = await self.app.pool.fetchval(
            """SELECT COUNT(DISTINCT COALESCE(target_chat_id,-id))
               FROM link_targets
               WHERE disposition='active' AND access_status='not_joined'
                 AND kind='group' AND request_needed IS NOT TRUE"""
        )
        return {
            "active": int(row["active"] or 0),
            "review": int(row["review"] or 0),
            "ready": int(row["ready"] or 0),
            "candidates": int(candidates or 0),
        }

    async def show_dashboard(self, event) -> None:
        radar_state, _, _ = self._module_state("radar")
        pv_state, _, _ = self._module_state("pv_reply")
        group_state, _, _ = self._module_state("group_reply")
        text = (
            "🔎 GR OBSERVER — PAINEL\n\n"
            f"📡 Radar: {radar_state}\n"
            f"💬 Atendimento PV: {pv_state}\n"
            f"👥 Grupos de chat: {group_state}\n\n"
            "Foco operacional: grupos em que sua conta está dentro e pode conversar."
        )
        buttons = [
            [
                Button.inline("📡 Radar", b"menu:radar"),
                Button.inline("💬 Atendimento PV", b"menu:pv"),
            ],
            [
                Button.inline("👥 Grupos", b"menu:groups"),
                Button.inline("⚙️ Sistema", b"menu:system"),
            ],
            [Button.inline("📊 Status completo", b"menu:status")],
        ]
        await self._render_menu(event, text, buttons)

    async def show_radar_menu(self, event) -> None:
        state, reason, enabled = self._module_state("radar")
        counts = await self._group_counts()
        text = (
            "📡 RADAR — GRUPOS\n\n"
            f"Estado: {state}\n"
            f"Motivo: {reason}\n\n"
            "O Radar confirma presença e permissão de escrita passivamente. "
            "Canais e inventário técnico ficam fora da operação diária."
        )
        buttons = [
            [self._toggle_button("radar", enabled, "Radar")],
            [
                Button.inline(
                    f"👥 Para conhecer ({counts['candidates']})",
                    b"menu:groups:list:candidates:0",
                ),
                Button.inline(
                    f"🟡 Revisar ({counts['review']})",
                    b"menu:groups:list:review:0",
                ),
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
        counts = await self._group_counts()
        text = (
            "👥 GRUPOS DE CHAT\n\n"
            f"Estado: {state}\n"
            f"Motivo: {reason}\n\n"
            "🟢 Ativo = ainda dentro + pode escrever + já tem postagem-modelo.\n"
            "🟡 Revisar = ainda dentro + já postou antes + escrita bloqueada ou incerta.\n"
            "✅ Livre = ainda dentro + pode escrever + falta cadastrar a postagem-modelo."
        )
        buttons = [
            [self._toggle_button("group_reply", enabled, "Grupos")],
            [
                Button.inline(
                    f"🟢 Ativos ({counts['active']})",
                    b"menu:groups:list:active:0",
                ),
                Button.inline(
                    f"🟡 Revisar ({counts['review']})",
                    b"menu:groups:list:review:0",
                ),
            ],
            [
                Button.inline(
                    f"✅ Livres, falta publicar ({counts['ready']})",
                    b"menu:groups:list:ready:0",
                )
            ],
            [
                Button.inline(
                    f"👥 Para conhecer ({counts['candidates']})",
                    b"menu:groups:list:candidates:0",
                )
            ],
            [Button.inline("📋 Configuração", b"menu:groups:preview")],
            [Button.inline("↩️ Início", b"home")],
        ]
        await self._render_menu(event, text, buttons)

    async def show_group_list(self, event, section: str, offset: int = 0) -> None:
        limit = self.GROUP_PAGE_SIZE
        offset = max(0, int(offset))
        rows = []
        buttons = []

        if section in {"active", "review", "ready"}:
            conditions = {
                "active": (
                    "c.kind='group' AND c.membership_status='joined' "
                    "AND c.disposition='active' AND c.can_text IS TRUE "
                    "AND r.chat_id IS NOT NULL"
                ),
                "review": (
                    "c.kind='group' AND c.membership_status='joined' "
                    "AND c.disposition='active' AND c.can_text IS NOT TRUE "
                    "AND r.chat_id IS NOT NULL"
                ),
                "ready": (
                    "c.kind='group' AND c.membership_status='joined' "
                    "AND c.disposition='active' AND c.can_text IS TRUE "
                    "AND r.chat_id IS NULL"
                ),
            }
            rows = await self.app.pool.fetch(
                f"""SELECT c.chat_id,c.title,c.can_text,c.last_scanned,
                    r.last_post_at,r.last_human_at
                    FROM chats c
                    LEFT JOIN group_repost_state r USING(chat_id)
                    WHERE {conditions[section]}
                    ORDER BY c.title OFFSET $1 LIMIT $2""",
                offset,
                limit,
            )
            title = {
                "active": "🟢 GRUPOS ATIVOS",
                "review": "🟡 GRUPOS PARA REVISAR",
                "ready": "✅ DENTRO E LIVRE — FALTA PUBLICAR",
            }[section]
            lines = [title, ""]
            for row in rows:
                name = str(row["title"] or row["chat_id"])
                if section == "active":
                    lines.append(
                        f"🟢 {name}\nÚltima postagem: {row['last_post_at'] or 'sem registro'}"
                    )
                elif section == "review":
                    reason = "escrita bloqueada" if row["can_text"] is False else "permissão incerta"
                    lines.append(
                        f"🟡 {name}\nMotivo: {reason} · consulta: {row['last_scanned'] or 'pendente'}"
                    )
                else:
                    lines.append(f"✅ {name}\nDentro e com escrita liberada.")
                buttons.append(
                    [Button.inline(name[:60], f"menu:groups:chat:{row['chat_id']}".encode())]
                )
        elif section == "candidates":
            rows = await self.app.pool.fetch(
                """SELECT (ARRAY_AGG(id ORDER BY last_checked DESC NULLS LAST))[1] id,
                       COALESCE(MAX(title),MIN(url)) title,
                       (ARRAY_AGG(url ORDER BY last_checked DESC NULLS LAST))[1] url
                   FROM link_targets
                   WHERE disposition='active' AND access_status='not_joined'
                     AND kind='group' AND request_needed IS NOT TRUE
                   GROUP BY COALESCE(target_chat_id,-id)
                   ORDER BY MAX(last_checked) DESC NULLS LAST
                   OFFSET $1 LIMIT $2""",
                offset,
                limit,
            )
            lines = ["👥 GRUPOS PARA CONHECER", "", "Entrada continua manual."]
            for row in rows:
                name = str(row["title"] or "Grupo")
                lines.append(f"👥 {name}")
                buttons.append(
                    [Button.inline(name[:60], f"menu:groups:candidate:{row['id']}".encode())]
                )
        else:
            await self.show_groups_menu(event)
            return

        nav = []
        if offset:
            nav.append(
                Button.inline(
                    "⬅️",
                    f"menu:groups:list:{section}:{max(0, offset-limit)}".encode(),
                )
            )
        if len(rows) == limit:
            nav.append(
                Button.inline(
                    "➡️", f"menu:groups:list:{section}:{offset+limit}".encode()
                )
            )
        if nav:
            buttons.append(nav)
        buttons.append([Button.inline("↩️ Grupos", b"menu:groups")])
        await self._render_menu(event, "\n\n".join(lines), buttons)

    async def show_group_health(self, event, chat_id: int) -> None:
        row = await self.app.pool.fetchrow(
            """SELECT c.chat_id,c.title,c.membership_status,c.can_text,c.last_scanned,
                      r.last_post_at,r.last_human_at,r.inbound_count
               FROM chats c LEFT JOIN group_repost_state r USING(chat_id)
               WHERE c.chat_id=$1""",
            int(chat_id),
        )
        if not row:
            await event.answer("Grupo não encontrado.", alert=True)
            return

        has_repost = row["last_post_at"] is not None or row["inbound_count"] is not None
        if row["membership_status"] != "joined":
            status = "🔴 Fora do grupo"
        elif row["can_text"] is True and has_repost:
            status = "🟢 Ativo"
        elif row["can_text"] is True:
            status = "✅ Dentro e livre — falta publicar"
        elif row["can_text"] is False:
            status = "🟡 Dentro, mas escrita bloqueada"
        else:
            status = "🟡 Dentro, permissão incerta"

        text = (
            f"👥 {row['title'] or row['chat_id']}\n\n"
            f"Estado: {status}\n"
            f"Estou dentro: {'sim' if row['membership_status'] == 'joined' else 'não'}\n"
            f"Posso escrever: {'sim' if row['can_text'] is True else 'não' if row['can_text'] is False else 'incerto'}\n"
            f"Última consulta: {row['last_scanned'] or 'pendente'}\n"
            f"Última postagem: {row['last_post_at'] or 'sem registro'}\n"
            f"Última atividade humana vista: {row['last_human_at'] or 'sem registro'}"
        )
        await self._render_menu(
            event,
            text,
            [[Button.inline("↩️ Grupos", b"menu:groups")]],
        )

    async def show_candidate_group(self, event, link_id: int) -> None:
        row = await self.app.pool.fetchrow(
            """SELECT id,COALESCE(title,'Grupo') title,url,access_status,
                      request_needed,last_checked
               FROM link_targets WHERE id=$1 AND kind='group'""",
            int(link_id),
        )
        if not row:
            await event.answer("Grupo não encontrado.", alert=True)
            return
        url = str(row["url"] or "")
        open_url = url if url.startswith(("http://", "https://")) else f"https://{url}"
        text = (
            f"👥 {row['title']}\n\n"
            "Ainda não está na sua lista de grupos.\n"
            "Entrada: manual. Depois que entrar, o Radar confirma presença e escrita.\n"
            f"Última consulta: {row['last_checked'] or 'pendente'}"
        )
        buttons = []
        if url:
            buttons.append([Button.url("➡️ Abrir grupo", open_url)])
        buttons.append([Button.inline("↩️ Para conhecer", b"menu:groups:list:candidates:0")])
        await self._render_menu(event, text, buttons)

    async def show_system_menu(self, event) -> None:
        _, _, botson_enabled = self._module_state("botson")
        text = (
            "⚙️ SISTEMA\n\n"
            "Estado geral, módulos e homologação.\n"
            "A operação diária fica em Radar, PV e Grupos."
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
            "menu:inventory": self.show_groups_menu,
            "menu:organizer": self.show_groups_menu,
            "organizer": self.show_groups_menu,
            "groups": self.show_groups_menu,
            "channels": self.show_groups_menu,
            "links": self.show_groups_menu,
            "postable": self.show_groups_menu,
            "uncertain": self.show_groups_menu,
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

        list_match = re.fullmatch(
            r"menu:groups:list:(active|review|ready|candidates):(\d+)", data
        )
        if list_match:
            section, raw_offset = list_match.groups()
            await event.answer()
            await self.show_group_list(event, section, int(raw_offset))
            return

        legacy_list = re.fullmatch(
            r"list:(posting_active|joined_closed|joined_pending|candidate_groups):(\d+)",
            data,
        )
        if legacy_list:
            old_section, raw_offset = legacy_list.groups()
            section = {
                "posting_active": "active",
                "joined_closed": "review",
                "joined_pending": "ready",
                "candidate_groups": "candidates",
            }[old_section]
            await event.answer()
            await self.show_group_list(event, section, int(raw_offset))
            return

        if re.fullmatch(r"list:(candidate_channels|followed_sources|unavailable):\d+", data):
            await event.answer("Fora do escopo operacional. Mostrando grupos.", alert=True)
            await self.show_groups_menu(event)
            return

        chat_match = re.fullmatch(r"menu:groups:chat:(-?\d+)", data)
        if chat_match:
            await event.answer()
            await self.show_group_health(event, int(chat_match.group(1)))
            return

        candidate_match = re.fullmatch(r"menu:groups:candidate:(\d+)", data)
        if candidate_match:
            await event.answer()
            await self.show_candidate_group(event, int(candidate_match.group(1)))
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
