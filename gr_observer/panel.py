"""Administrative control-bot adapter."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from telethon import Button, events

from .catalog import BRAND, COMMANDS, DIALOGS, match_command
from .domain import source_label


class ControlPanel:
    def __init__(self, application):
        self.app = application
        self.client = application.panel
        self.awaiting_live_link = False
        self.pending_live_link: str | None = None

    def register_handlers(self) -> None:
        self.client.add_event_handler(self.on_message, events.NewMessage())
        self.client.add_event_handler(self.on_callback, events.CallbackQuery())

    async def on_message(self, event) -> None:
        if not self.app.is_admin(event):
            return
        if self.awaiting_live_link:
            link = (event.raw_text or "").strip()
            parsed = urlparse(link)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                await event.respond(
                    "Link inválido. Envie o endereço completo começando com http:// ou https://",
                    parse_mode=None,
                )
                return
            self.awaiting_live_link = False
            self.pending_live_link = link
            subscribed, pending = await self.app.storage.live_subscription_counts()
            await event.respond(
                "🔴 CONFIRMAR AVISO DE LIVE\n\n"
                f"Inscritos: {subscribed}\nAguardando consentimento: {pending}\n"
                "O link só será enviado a quem responder positivamente.",
                buttons=[
                    [Button.inline("✅ Enviar convites", b"live:send")],
                    [Button.inline("❌ Cancelar", b"live:cancel")],
                ],
                parse_mode=None,
                link_preview=False,
            )
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
        if data == "live:new":
            item = self.app.registry.get("pv_reply")
            if not item.enabled:
                await event.answer("Ligue o Atendimento PV primeiro.", alert=True)
                return
            self.pending_live_link = None
            self.awaiting_live_link = True
            await event.answer()
            await event.respond(
                "🔴 NOVA LIVE\n\nEnvie agora o link completo da live.",
                buttons=[[Button.inline("❌ Cancelar", b"live:cancel")]],
                parse_mode=None,
                link_preview=False,
            )
            return
        if data == "live:cancel":
            self.pending_live_link = None
            self.awaiting_live_link = False
            await event.answer("Campanha cancelada.", alert=True)
            await self.show_dashboard(event)
            return
        if data == "live:send":
            if not self.pending_live_link:
                await event.answer("Link expirou. Comece uma nova live.", alert=True)
                return
            link, self.pending_live_link = self.pending_live_link, None
            campaign_id, total = await self.app.storage.create_live_campaign(
                created_by=self.app.admin_id,
                link=link,
            )
            await event.answer("Campanha criada.", alert=True)
            await event.respond(
                f"🔴 Live #{campaign_id}: {total} convite(s) colocado(s) na fila.",
                parse_mode=None,
                link_preview=False,
            )
            return
        if data == "functions":
            await self.show_functions(event)
            return
        if data == "organizer":
            await self.show_organizer(event)
            return
        if data == "command:status":
            await event.answer()
            await event.respond(
                self.app.status_text(), parse_mode=None, link_preview=False
            )
            return
        if data == "command:pv_preview":
            await event.answer()
            await event.respond(
                self.app.registry.get("pv_reply").implementation.preview(),
                parse_mode=None,
                link_preview=False,
            )
            return
        if data == "home":
            await self.show_dashboard(event)
            return
        link_action = re.fullmatch(r"link:(discard|restore|refresh):(\d+)", data)
        if link_action:
            action, link_id = link_action.groups()
            link_id = int(link_id)
            if action == "refresh":
                radar = self.app.registry.get("radar")
                if not radar.enabled or "radar" not in self.app.connected_modules:
                    await event.answer("Ligue o Radar para atualizar.", alert=True)
                    return
                result = await radar.implementation.audit_link(link_id)
                await event.answer(f"Situação: {result}", alert=True)
            else:
                disposition = "discarded" if action == "discard" else "active"
                await self.app.pool.execute(
                    "UPDATE link_targets SET disposition=$2 WHERE id=$1",
                    link_id,
                    disposition,
                )
                await event.answer("Atualizado.", alert=True)
            await self.show_link(event, link_id)
            return
        chat_action = re.fullmatch(r"chat:(discard|restore|serve):(-?\d+)", data)
        if chat_action:
            action, chat_id = chat_action.groups()
            if action == "serve":
                await self.app.pool.execute(
                    """UPDATE chats SET can_text=TRUE, disposition='active',
                       membership_status='joined', last_scanned=NOW()
                       WHERE chat_id=$1""",
                    int(chat_id),
                )
            else:
                await self.app.pool.execute(
                    "UPDATE chats SET disposition=$2 WHERE chat_id=$1",
                    int(chat_id),
                    "discarded" if action == "discard" else "active",
                )
            await event.answer("Atualizado.", alert=True)
            await self.show_chat(event, int(chat_id))
            return
        link_detail = re.fullmatch(r"link:(\d+)", data)
        if link_detail:
            await self.show_link(event, int(link_detail.group(1)))
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
            "joined_postable",
            "joined_untested",
            "joined_readonly",
            "missing",
            "broken",
            "left",
            "discarded",
        }:
            await self.show_list(event, data, 0)
            return
        detail = re.fullmatch(r"chat:(-?\d+)", data)
        if detail:
            await self.show_chat(event, int(detail.group(1)))
            return
        listing = re.fullmatch(r"list:([a-z_]+):(\d+)", data)
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
            [Button.inline("🗂 Organizar grupos/canais", b"organizer")],
            [Button.inline("🔴 Nova live", b"live:new")],
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

    async def show_organizer(self, event) -> None:
        counts = await self.app.pool.fetchrow(
            """SELECT
               (SELECT COUNT(*) FROM chats WHERE membership_status='joined'
                 AND disposition='active' AND can_text IS TRUE) joined_postable,
               (SELECT COUNT(*) FROM chats WHERE membership_status='joined'
                 AND disposition='active' AND can_text IS NULL) joined_untested,
               (SELECT COUNT(*) FROM chats WHERE membership_status='joined'
                 AND disposition='active' AND can_text IS FALSE) joined_readonly,
               (SELECT COUNT(*) FROM link_targets WHERE disposition='active'
                 AND access_status='not_joined') missing,
               (SELECT COUNT(*) FROM link_targets WHERE disposition='active'
                 AND access_status IN ('invalid','inaccessible')) broken,
               (SELECT COUNT(*) FROM chats WHERE disposition='active'
                 AND membership_status='left') left_total,
               ((SELECT COUNT(*) FROM chats WHERE disposition='discarded')+
                (SELECT COUNT(*) FROM link_targets WHERE disposition='discarded')) discarded"""
        )
        buttons = [
            [Button.inline(f"✅ Dentro e pode postar ({counts['joined_postable']})", b"list:joined_postable:0")],
            [Button.inline(f"👀 Dentro, não testado ({counts['joined_untested']})", b"list:joined_untested:0")],
            [Button.inline(f"🚫 Dentro, somente leitura ({counts['joined_readonly']})", b"list:joined_readonly:0")],
            [Button.inline(f"➕ Falta entrar ({counts['missing']})", b"list:missing:0")],
            [Button.inline(f"❌ Link não abre ({counts['broken']})", b"list:broken:0")],
            [Button.inline(f"🚪 Saí/não estou mais ({counts['left_total']})", b"list:left:0")],
            [Button.inline(f"🗑️ Não serve/descartados ({counts['discarded']})", b"list:discarded:0")],
            [Button.inline("↩️ Início", b"home")],
        ]
        text = (
            "🗂 ORGANIZADOR DE GRUPOS E CANAIS\n\n"
            "O Radar apenas verifica e organiza. Ele não entra nem publica automaticamente."
        )
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
        else:
            await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)

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
            icon = "🟢" if item.enabled else "🔴"
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
            preferred = next(
                (value for value in spec["triggers"] if value.startswith("/")),
                spec["triggers"][0],
            )
            lines.append(f"• {preferred} — {spec['help']}")
        buttons += [
            [Button.inline("📊 Ver status", b"command:status")],
            [Button.inline("💬 Ver mensagens do PV", b"command:pv_preview")],
            [Button.inline("🔴 Nova live", b"live:new")],
            [Button.inline("🧪 Executar teste BOTSON", b"botson:run")],
        ]
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
        organizer_chat_where = {
            "joined_postable": "membership_status='joined' AND disposition='active' AND can_text IS TRUE",
            "joined_untested": "membership_status='joined' AND disposition='active' AND can_text IS NULL",
            "joined_readonly": "membership_status='joined' AND disposition='active' AND can_text IS FALSE",
            "left": "membership_status='left' AND disposition='active'",
        }
        if section in organizer_chat_where:
            rows = await self.app.pool.fetch(
                f"""SELECT chat_id,title label,can_text,kind FROM chats
                    WHERE {organizer_chat_where[section]}
                    ORDER BY title OFFSET $1 LIMIT $2""",
                offset,
                limit,
            )
            lines = [
                f"{'✅' if row['can_text'] else '🚫' if row['can_text'] is False else '👀'} "
                f"{row['label']} — {row['kind']}"
                for row in rows
            ]
            item_buttons = [
                [Button.inline(row["label"][:60], f"chat:{row['chat_id']}".encode())]
                for row in rows
            ]
        elif section in {"missing", "broken"}:
            condition = (
                "access_status='not_joined'"
                if section == "missing"
                else "access_status IN ('invalid','inaccessible')"
            )
            rows = await self.app.pool.fetch(
                f"""SELECT id,COALESCE(title,url) label,url,access_status
                    FROM link_targets WHERE disposition='active' AND {condition}
                    ORDER BY first_seen DESC OFFSET $1 LIMIT $2""",
                offset,
                limit,
            )
            lines = [
                f"{'➕' if row['access_status']=='not_joined' else '❌'} {row['label']}"
                for row in rows
            ]
            item_buttons = [
                [Button.inline(row["label"][:60], f"link:{row['id']}".encode())]
                for row in rows
            ]
        elif section == "discarded":
            chat_rows = await self.app.pool.fetch(
                """SELECT chat_id id,title label,'chat' item_type FROM chats
                   WHERE disposition='discarded' ORDER BY title"""
            )
            link_rows = await self.app.pool.fetch(
                """SELECT id,COALESCE(title,url) label,'link' item_type FROM link_targets
                   WHERE disposition='discarded' ORDER BY first_seen DESC"""
            )
            all_rows = [dict(row) for row in chat_rows] + [dict(row) for row in link_rows]
            rows = all_rows[offset : offset + limit]
            lines = [f"🗑️ {row['label']}" for row in rows]
            item_buttons = [
                [
                    Button.inline(
                        row["label"][:60],
                        f"{row['item_type']}:{row['id']}".encode(),
                    )
                ]
                for row in rows
            ]
        elif section == "bots":
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
        organizer_sections = {
            "joined_postable",
            "joined_untested",
            "joined_readonly",
            "missing",
            "broken",
            "left",
            "discarded",
        }
        back_target = b"organizer" if section in organizer_sections else b"home"
        buttons.append([Button.inline("↩️ Voltar", back_target)])
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
            f"{row['title']}\n\nSituação: {row['membership_status']}\n"
            f"Classificação: {row['disposition']}\nTexto: {label(row['can_text'])}\n"
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
        if row["disposition"] == "discarded":
            buttons.append(
                [Button.inline("♻️ Restaurar", f"chat:restore:{chat_id}".encode())]
            )
        else:
            if row["can_text"] is not True:
                buttons.append(
                    [Button.inline("✅ Marcar como serve", f"chat:serve:{chat_id}".encode())]
                )
            buttons.append(
                [Button.inline("🗑️ Marcar como não serve", f"chat:discard:{chat_id}".encode())]
            )
        buttons.append([Button.inline("↩️ Organizador", b"organizer")])
        await event.edit(
            text[:3800], buttons=buttons, parse_mode=None, link_preview=False
        )

    async def show_link(self, event, link_id: int) -> None:
        row = await self.app.pool.fetchrow(
            "SELECT * FROM link_targets WHERE id=$1", link_id
        )
        if not row:
            await event.answer("Link não encontrado", alert=True)
            return
        status_labels = {
            "joined": "✅ Já estou dentro",
            "not_joined": "➕ Falta entrar",
            "invalid": "❌ Link inválido ou vencido",
            "inaccessible": "⚠️ Não foi possível verificar",
        }
        text = (
            f"{row['title'] or 'Link do Telegram'}\n\n"
            f"Situação: {status_labels.get(row['access_status'], row['access_status'])}\n"
            f"Tipo: {row['kind'] or 'a confirmar'}\n"
            f"Última verificação: {row['last_checked'] or 'ainda não verificado'}\n"
            f"Erro: {row['last_error'] or 'nenhum'}\n\n{row['url']}"
        )
        buttons = [[Button.url("Abrir no Telegram", row["url"])]]
        buttons.append(
            [Button.inline("🔄 Atualizar situação", f"link:refresh:{link_id}".encode())]
        )
        if row["disposition"] == "discarded":
            buttons.append(
                [Button.inline("♻️ Restaurar", f"link:restore:{link_id}".encode())]
            )
        else:
            buttons.append(
                [Button.inline("🗑️ Marcar como não serve", f"link:discard:{link_id}".encode())]
            )
        buttons.append([Button.inline("↩️ Organizador", b"organizer")])
        await event.edit(text[:3800], buttons=buttons, parse_mode=None, link_preview=False)
