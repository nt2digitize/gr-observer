"""Administrative control-bot adapter."""

from __future__ import annotations

import re

from telethon import Button, events
from telethon.tl import types

from .catalog import BRAND, COMMANDS, DIALOGS, match_command
from .domain import source_label


def preferred_command_text(spec: dict) -> str:
    """Prefer the familiar slash form, falling back to an accepted phrase."""
    return next(
        (value for value in spec["triggers"] if value.startswith("/")),
        spec["triggers"][0],
    )


class ControlPanel:
    def __init__(self, application):
        self.app = application
        self.client = application.panel

    def register_handlers(self) -> None:
        self.client.add_event_handler(self.on_message, events.NewMessage())
        self.client.add_event_handler(self.on_callback, events.CallbackQuery())

    async def on_message(self, event) -> None:
        if not self.app.is_admin(event):
            return
        command_id = match_command(event.raw_text or "", "panel")
        if command_id == "core.dashboard":
            await self.show_dashboard(event)
        elif command_id == "core.status":
            await event.respond(
                self.app.status_text(), parse_mode=None, link_preview=False
            )
        elif command_id == "core.functions":
            await self.show_functions(event)
        elif command_id == "radar.enable":
            await event.respond(
                await self.app.set_module_enabled("radar", True), parse_mode=None
            )
        elif command_id == "radar.disable":
            await event.respond(
                await self.app.set_module_enabled("radar", False), parse_mode=None
            )
        elif command_id == "pv_reply.enable":
            await event.respond(
                await self.app.set_module_enabled("pv_reply", True), parse_mode=None
            )
        elif command_id == "pv_reply.disable":
            await event.respond(
                await self.app.set_module_enabled("pv_reply", False), parse_mode=None
            )
        elif command_id == "pv_reply.preview":
            await event.respond(
                self.app.registry.get("pv_reply").implementation.preview(),
                parse_mode=None,
                link_preview=False,
            )
        elif command_id == "botson.enable":
            await event.respond(
                await self.app.set_module_enabled("botson", True), parse_mode=None
            )
        elif command_id == "botson.disable":
            await event.respond(
                await self.app.set_module_enabled("botson", False), parse_mode=None
            )
        elif command_id == "botson.run":
            await self._request_botson(event)

    async def _request_botson(self, event) -> None:
        item = self.app.registry.get("botson")
        if not item.enabled:
            await event.respond("Testar BOTSON está desligado. Envie: ligar botson")
            return
        outcome = await item.implementation.enqueue_panel_run(
            f"{event.chat_id}:{event.id}"
        )
        messages = {
            "accepted": "Teste colocado na fila. O resultado chegará no privado da conta controladora.",
            "busy": DIALOGS["botson.busy"],
            "duplicate": "Esse pedido já foi recebido.",
        }
        await event.respond(messages.get(outcome, outcome), parse_mode=None)

    async def on_callback(self, event) -> None:
        if not self.app.is_admin(event):
            await event.answer(DIALOGS["admin.no_authorization"], alert=True)
            return
        data = event.data.decode("utf-8", errors="replace")
        if data in {"power:on", "power:off"}:
            result = await self.app.set_module_enabled("radar", data == "power:on")
            await event.answer(result[:200], alert=True)
            await self.show_dashboard(event)
            return
        module_toggle = re.fullmatch(
            r"module:(radar|pv_reply|botson):(on|off)", data
        )
        if module_toggle:
            module_id, direction = module_toggle.groups()
            result = await self.app.set_module_enabled(module_id, direction == "on")
            await event.answer(result[:200], alert=True)
            await self.show_functions(event)
            return
        if data == "botson:run":
            await event.answer()
            await self._request_botson(event)
            return
        if data == "functions":
            await self.show_functions(event)
            return
        if data == "home":
            await self.show_dashboard(event)
            return
        if data in {
            "groups",
            "channels",
            "postable",
            "uncertain",
            "bots",
            "links",
            "origins",
            "drafts",
        }:
            await self.show_list(event, data, 0)
            return
        detail = re.fullmatch(r"chat:(-?\d+)", data)
        if detail:
            await self.show_chat(event, int(detail.group(1)))
            return
        listing = re.fullmatch(r"list:([a-z]+):(\d+)", data)
        if listing:
            await self.show_list(event, listing.group(1), int(listing.group(2)))

    async def show_dashboard(self, event) -> None:
        counts = await self.app.storage.counts_for_dashboard()
        bots = await self.app.pool.fetchval(
            "SELECT COUNT(DISTINCT bot_id) FROM observed_bots"
        )
        links = await self.app.pool.fetchval(
            "SELECT COUNT(*) FROM discovered_links WHERE status='pending'"
        )
        text = (
            f"{BRAND['panel']}\n{self.app.status_text()}\n\n"
            f"📢 Canais: {counts['channels']}\n👥 Grupos: {counts['groups']}\n"
            f"✅ Pode postar: {counts['postable']}\n⚠️ Incertos: {counts['uncertain']}\n"
            f"🤖 Bots: {bots}\n🔗 Links aguardando: {links}"
        )
        buttons = [
            [Button.inline(f"📢 Canais ({counts['channels']})", b"channels")],
            [Button.inline(f"👥 Grupos ({counts['groups']})", b"groups")],
            [Button.inline(f"✅ Postáveis ({counts['postable']})", b"postable")],
            [Button.inline(f"⚠️ Incertos ({counts['uncertain']})", b"uncertain")],
            [Button.inline(f"🤖 Bots ({bots})", b"bots")],
            [Button.inline(f"🔗 Links ({links})", b"links")],
            [Button.inline("👤 Origens PV", b"origins")],
            [Button.inline("📝 Rascunhos", b"drafts")],
            [Button.inline("🧩 Funções", b"functions")],
            [Button.inline("🧪 Testar BOTSON", b"botson:run")],
            [Button.inline("▶️ Ligar observação", b"power:on")],
            [Button.inline("⏸️ Desligar observação", b"power:off")],
        ]
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
        else:
            await event.respond(
                text, buttons=buttons, parse_mode=None, link_preview=False
            )

    async def show_functions(self, event) -> None:
        lines = [
            "🧩 FUNÇÕES — ESPINHA DE PEIXE",
            "",
            "Coluna central: sessão + Inbox/Outbox + Writer",
            "",
        ]
        buttons = []
        for item in self.app.registry.ordered():
            state = "LIGADA" if item.enabled else "DESLIGADA"
            icon = "🟢" if item.enabled else "⚪"
            lines += [
                f"{item.spec['order']}. {icon} {item.spec['label']} — {state}",
                item.spec["description"],
                f"Motivo: {item.reason}",
                "",
            ]
            direction = "off" if item.enabled else "on"
            verb = "Desligar" if item.enabled else "Ligar"
            buttons.append(
                [
                    Button.inline(
                        f"{verb} {item.spec['label']}",
                        f"module:{item.module_id}:{direction}".encode(),
                    )
                ]
            )
        lines.append("Comandos de texto:")
        for _, spec in sorted(COMMANDS.items(), key=lambda entry: entry[1]["order"]):
            if "panel" not in spec["surfaces"]:
                continue
            preferred = preferred_command_text(spec)
            lines.append(f"• {preferred} — {spec['help']}")
            buttons.append(
                [
                    types.KeyboardButtonCopy(
                        text=f"📋 Copiar {preferred}",
                        copy_text=preferred,
                    )
                ]
            )
        buttons.append([Button.inline("↩️ Início", b"home")])
        text = "\n".join(lines)
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(
                text[:3900], buttons=buttons, parse_mode=None, link_preview=False
            )
        else:
            await event.respond(
                text[:3900], buttons=buttons, parse_mode=None, link_preview=False
            )

    async def show_list(self, event, section: str, offset: int) -> None:
        limit = 8
        item_buttons = []
        if section == "bots":
            rows = await self.app.pool.fetch(
                """SELECT COALESCE(username,display_name,'bot') label, COUNT(*) total
                   FROM observed_bots GROUP BY 1 ORDER BY total DESC OFFSET $1 LIMIT $2""",
                offset,
                limit,
            )
            lines = [f"🤖 @{row['label']} — {row['total']} grupo(s)" for row in rows]
        elif section == "links":
            rows = await self.app.pool.fetch(
                """SELECT l.url label, l.chat_id source_id, c.title source_title
                   FROM discovered_links l LEFT JOIN chats c ON c.chat_id=l.chat_id
                   WHERE l.status='pending' ORDER BY l.observed_at DESC OFFSET $1 LIMIT $2""",
                offset,
                limit,
            )
            lines = [
                f"🔗 {row['label']}\n   origem: {source_label(row['source_title'], row['source_id'])}"
                for row in rows
            ]
        elif section == "origins":
            rows = await self.app.pool.fetch(
                """SELECT COALESCE(p.username,p.user_id::text) label,
                   p.source_chat_id source_id, c.title source_title
                   FROM private_origins p LEFT JOIN chats c ON c.chat_id=p.source_chat_id
                   ORDER BY p.detected_at DESC OFFSET $1 LIMIT $2""",
                offset,
                limit,
            )
            lines = [
                f"👤 @{row['label']} ← {source_label(row['source_title'], row['source_id'])}"
                for row in rows
            ]
        elif section == "drafts":
            rows = await self.app.pool.fetch(
                """SELECT reason label, user_id total FROM drafts WHERE status='pending'
                   ORDER BY created_at DESC OFFSET $1 LIMIT $2""",
                offset,
                limit,
            )
            lines = [f"📝 {row['label']} — usuário {row['total']}" for row in rows]
        else:
            where = {
                "groups": "kind='group'",
                "channels": "kind='channel'",
                "postable": "can_text IS TRUE",
                "uncertain": "can_text IS NULL OR risk IN ('unknown','medium')",
            }[section]
            rows = await self.app.pool.fetch(
                f"""SELECT chat_id,title label,can_text,can_media,can_links FROM chats
                    WHERE {where} ORDER BY title OFFSET $1 LIMIT $2""",
                offset,
                limit,
            )
            lines = [
                (
                    "🟢"
                    if row["can_text"]
                    else "🔴"
                    if row["can_text"] is False
                    else "⚪"
                )
                + f" {row['label']} | texto={row['can_text']} mídia={row['can_media']} links={row['can_links']}"
                for row in rows
            ]
            item_buttons = [
                [Button.inline(row["label"][:60], f"chat:{row['chat_id']}".encode())]
                for row in rows
            ]
        navigation = []
        if offset:
            navigation.append(
                Button.inline("⬅️", f"list:{section}:{max(0, offset - limit)}".encode())
            )
        if len(rows) == limit:
            navigation.append(
                Button.inline("➡️", f"list:{section}:{offset + limit}".encode())
            )
        buttons = item_buttons + ([navigation] if navigation else [])
        buttons.append([Button.inline("↩️ Início", b"home")])
        await event.edit(
            "\n\n".join(lines) if lines else DIALOGS["admin.empty_list"],
            buttons=buttons,
            parse_mode=None,
            link_preview=False,
        )

    async def show_chat(self, event, chat_id: int) -> None:
        row = await self.app.pool.fetchrow(
            "SELECT * FROM chats WHERE chat_id=$1", chat_id
        )
        if not row:
            await event.answer("Grupo não encontrado")
            return

        def label(value):
            return "sim" if value is True else "não" if value is False else "incerto"

        rules = await self.app.pool.fetch(
            "SELECT excerpt FROM visible_rules WHERE chat_id=$1 ORDER BY observed_at DESC LIMIT 3",
            chat_id,
        )
        bots = await self.app.pool.fetch(
            """SELECT COALESCE(username,display_name,'bot') label FROM observed_bots
               WHERE chat_id=$1 LIMIT 8""",
            chat_id,
        )
        text = (
            f"{row['title']}\n\nTexto: {label(row['can_text'])}\n"
            f"Mídia (geral): {label(row['can_media'])}\n"
            f"Links/divulgação: {label(row['can_links'])}\n"
            f"Slow mode: {row['slowmode_seconds']} s\nÚltima consulta: {row['last_scanned']}\n\n"
            "Trechos candidatos a regras (revisar):\n"
            + "\n".join(item["excerpt"][:400] for item in rules)
            + "\n\nBots observados: "
            + ", ".join(item["label"] for item in bots)
        )
        buttons = []
        if row["username"]:
            buttons.append(
                [Button.url("Abrir no Telegram", f"https://t.me/{row['username']}")]
            )
        elif chat_id < -1000000000000:
            buttons.append(
                [
                    Button.url(
                        "Abrir no Telegram",
                        f"https://t.me/c/{-chat_id - 1000000000000}/1",
                    )
                ]
            )
        buttons.append([Button.inline("Voltar ao painel", b"home")])
        await event.edit(
            text[:3800], buttons=buttons, parse_mode=None, link_preview=False
        )
