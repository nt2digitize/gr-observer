import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone

import asyncpg
from dotenv import load_dotenv
from telethon import Button, TelegramClient, events, functions, types, utils, errors
from telethon.sessions import MemorySession, StringSession

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("gr-observer")

RULE_WORDS = re.compile(r"\b(regras?|proibid[oa]s?|não pode|nao pode|ban|divulga(?:ção|cao)|flood|spam|links?|pv|privado)\b", re.I)
URL_RE = re.compile(r"(?:https?://|t\.me/|telegram\.me/)[^\s<>]+", re.I)

# Mensagens destinadas a usuários devem seguir estilo de chat nativo. Textos do
# painel administrativo ficam formais para não prejudicar a leitura operacional.
USER_MESSAGE_TEMPLATES = {
    "private_link_request": "pera aí q vou ver o link certo p vc",
}


def required(name):
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Variável obrigatória ausente: {name}")
    return value


def now():
    return datetime.now(timezone.utc)


def title_of(entity):
    return getattr(entity, "title", None) or getattr(entity, "username", None) or str(entity.id)


def kind_of(entity):
    if isinstance(entity, types.Channel) and entity.broadcast:
        return "channel"
    if isinstance(entity, (types.Channel, types.Chat)):
        return "group"
    return "private"


def source_label(title, chat_id):
    return f"{title} ({chat_id})" if title else str(chat_id)


SCHEMA = """
CREATE TABLE IF NOT EXISTS observer_control (
  singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(singleton),
  enabled BOOLEAN NOT NULL DEFAULT FALSE, reason TEXT NOT NULL DEFAULT 'Pausado',
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
INSERT INTO observer_control(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS chats (
  chat_id BIGINT PRIMARY KEY, title TEXT NOT NULL, username TEXT, kind TEXT NOT NULL,
  can_text BOOLEAN, can_media BOOLEAN, can_links BOOLEAN, slowmode_seconds INTEGER,
  risk TEXT NOT NULL DEFAULT 'unknown', last_scanned TIMESTAMPTZ, last_seen TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS visible_rules (
  chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, excerpt TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS observed_bots (
  chat_id BIGINT NOT NULL, bot_id BIGINT NOT NULL, username TEXT, display_name TEXT,
  evidence TEXT, last_seen TIMESTAMPTZ NOT NULL, PRIMARY KEY(chat_id, bot_id)
);
CREATE TABLE IF NOT EXISTS discovered_links (
  chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL, url TEXT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  PRIMARY KEY(chat_id, message_id, url)
);
CREATE TABLE IF NOT EXISTS group_interactions (
  user_id BIGINT NOT NULL, chat_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
  interaction_type TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL,
  PRIMARY KEY(user_id, chat_id, message_id)
);
CREATE TABLE IF NOT EXISTS private_origins (
  user_id BIGINT PRIMARY KEY, username TEXT, source_chat_id BIGINT,
  confidence TEXT NOT NULL, detected_at TIMESTAMPTZ NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
  id BIGSERIAL PRIMARY KEY, chat_id BIGINT, user_id BIGINT, reason TEXT NOT NULL,
  suggested_text TEXT NOT NULL, media_key TEXT, status TEXT NOT NULL DEFAULT 'pending',
  created_at TIMESTAMPTZ NOT NULL
);
"""


class Observer:
    def __init__(self):
        self.api_id = int(required("TELEGRAM_API_ID"))
        self.api_hash = required("TELEGRAM_API_HASH")
        self.admin_id = int(required("ADMIN_USER_ID"))
        self.db_url = required("DATABASE_URL").replace("postgres://", "postgresql://", 1)
        self.history_limit = max(0, min(50, int(os.getenv("HISTORY_LIMIT", "20"))))
        self.scan_minutes = int(os.getenv("SCAN_INTERVAL_MINUTES", "360"))
        self.user = None
        self.panel = TelegramClient(MemorySession(), self.api_id, self.api_hash, flood_sleep_threshold=0)
        self.pool = None
        self.me = None
        self.enabled = False
        self.worker = None
        self.control_lock = asyncio.Lock()
        self.active_events = set()
        self.pause_tasks = set()
        self.state_reason = "Pausado"

    async def setup(self):
        self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=4)
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)
        await self.panel.start(bot_token=required("CONTROL_BOT_TOKEN"))
        self.register_handlers()
        state = await self.pool.fetchrow("SELECT enabled, reason FROM observer_control WHERE singleton=TRUE")
        self.state_reason = state["reason"]
        if state["enabled"]:
            await self.set_enabled(True)

    async def save_chat(self, entity, **values):
        chat_id = utils.get_peer_id(entity)
        await self.pool.execute(
            """INSERT INTO chats(chat_id,title,username,kind,last_seen) VALUES($1,$2,$3,$4,$5)
            ON CONFLICT(chat_id) DO UPDATE SET title=$2,username=$3,kind=$4,last_seen=$5""",
            chat_id, title_of(entity), getattr(entity, "username", None), kind_of(entity), now())
        if values:
            allowed = {"can_text", "can_media", "can_links", "slowmode_seconds", "risk", "last_scanned"}
            clean = {k: v for k, v in values.items() if k in allowed}
            for key, value in clean.items():
                await self.pool.execute(f"UPDATE chats SET {key}=$1 WHERE chat_id=$2", value, chat_id)

    async def inspect_permissions(self, entity):
        kind = kind_of(entity)
        can_text = can_media = can_links = None
        slowmode = None
        try:
            permissions = await self.user.get_permissions(entity, self.me)
            if kind == "channel":
                rights = getattr(entity, "admin_rights", None)
                can_text = bool(getattr(entity, "creator", False) or (rights and getattr(rights, "post_messages", False)))
                can_media = can_text
                can_links = None if can_text else False
            else:
                can_text = getattr(permissions, "send_messages", None)
                can_media = getattr(permissions, "send_media", None) if can_text is True else can_text
                # Link previews are not evidence that advertising/URLs are allowed.
                can_links = False if can_text is False else None
            try:
                full = await self.user(functions.channels.GetFullChannelRequest(entity))
                slowmode = getattr(full.full_chat, "slowmode_seconds", None)
            except errors.FloodWaitError:
                raise
            except Exception:
                pass
        except errors.FloodWaitError:
            raise
        except Exception as exc:
            log.info("Permissão incerta chat=%s erro=%s", entity.id, type(exc).__name__)
        risk = "low" if can_text and can_links else "medium" if can_text else "blocked" if can_text is False else "unknown"
        await self.save_chat(entity, can_text=can_text, can_media=can_media, can_links=can_links,
                             slowmode_seconds=slowmode, risk=risk, last_scanned=now())

    async def inspect_history(self, entity):
        async for message in self.user.iter_messages(entity, limit=self.history_limit):
            text = message.raw_text or ""
            if text and RULE_WORDS.search(text):
                await self.pool.execute(
                    """INSERT INTO visible_rules(chat_id,message_id,excerpt,observed_at) VALUES($1,$2,$3,$4)
                    ON CONFLICT DO NOTHING""", utils.get_peer_id(entity), message.id, text[:600], now())
            await self.capture_links(utils.get_peer_id(entity), message.id, text)
            sender = await message.get_sender()
            await self.capture_bot(utils.get_peer_id(entity), sender, "history")

    async def capture_links(self, chat_id, message_id, text):
        for url in URL_RE.findall(text or ""):
            await self.pool.execute(
                """INSERT INTO discovered_links(chat_id,message_id,url,observed_at) VALUES($1,$2,$3,$4)
                ON CONFLICT DO NOTHING""", chat_id, message_id, url.rstrip(".,);]"), now())

    async def capture_bot(self, chat_id, sender, evidence):
        if not sender or not getattr(sender, "bot", False):
            return
        display = " ".join(filter(None, [getattr(sender, "first_name", None), getattr(sender, "last_name", None)]))
        await self.pool.execute(
            """INSERT INTO observed_bots(chat_id,bot_id,username,display_name,evidence,last_seen)
            VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(chat_id,bot_id) DO UPDATE SET
            username=$3,display_name=$4,evidence=$5,last_seen=$6""",
            chat_id, sender.id, getattr(sender, "username", None), display, evidence, now())

    async def scan_all(self):
        async for dialog in self.user.iter_dialogs():
            entity = dialog.entity
            if kind_of(entity) not in {"group", "channel"}:
                continue
            try:
                await self.save_chat(entity)
                await self.inspect_permissions(entity)
                await self.inspect_history(entity)
                await asyncio.sleep(1.2)
            except errors.FloodWaitError:
                raise
            except Exception as exc:
                log.warning("Falha de leitura chat=%s erro=%s", entity.id, type(exc).__name__)

    async def scan_loop(self):
        while True:
            await self.pool.execute("DELETE FROM group_interactions WHERE observed_at < NOW() - INTERVAL '7 days'")
            await self.pool.execute("DELETE FROM private_origins WHERE detected_at < NOW() - INTERVAL '7 days'")
            await self.pool.execute("DELETE FROM drafts WHERE created_at < NOW() - INTERVAL '7 days'")
            await self.scan_all()
            await asyncio.sleep(self.scan_minutes * 60)

    async def detect_directed(self, event, sender):
        text = event.raw_text or ""
        mentioned = bool(self.me.username and f"@{self.me.username.lower()}" in text.lower())
        replied = False
        if event.is_reply:
            original = await event.get_reply_message()
            replied = bool(original and original.sender_id == self.me.id)
        if sender and not getattr(sender, "bot", False) and (mentioned or replied):
            await self.pool.execute(
                """INSERT INTO group_interactions(user_id,chat_id,message_id,interaction_type,observed_at)
                VALUES($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING""",
                sender.id, event.chat_id, event.id, "reply" if replied else "mention", now())

    async def detect_private_origin(self, event, sender):
        if not sender or getattr(sender, "bot", False):
            return
        row = await self.pool.fetchrow(
            """SELECT chat_id FROM group_interactions WHERE user_id=$1
            AND observed_at > NOW() - INTERVAL '7 days' ORDER BY observed_at DESC LIMIT 1""", sender.id)
        if not row:
            return
        await self.pool.execute(
            """INSERT INTO private_origins(user_id,username,source_chat_id,confidence,detected_at)
            VALUES($1,$2,$3,'probable',$4) ON CONFLICT(user_id) DO UPDATE SET
            username=$2,source_chat_id=$3,confidence='probable',detected_at=$4""",
            sender.id, getattr(sender, "username", None), row["chat_id"], now())
        if re.search(r"\b(link|grupo|canal|vip|pv|privado)\b", event.raw_text or "", re.I):
            await self.pool.execute(
                """INSERT INTO drafts(chat_id,user_id,reason,suggested_text,status,created_at)
                VALUES($1,$2,'pedido de link no privado',$3,'pending',$4)""",
                row["chat_id"], sender.id,
                USER_MESSAGE_TEMPLATES["private_link_request"], now())

    def is_admin(self, event):
        return event.sender_id == self.admin_id and event.is_private

    async def set_enabled(self, enabled, reason="Pausado pelo administrador"):
        async with self.control_lock:
            if enabled:
                if self.worker and not self.worker.done():
                    return "O Radar já está ligado ou conectando."
                if not os.getenv("USER_SESSION_STRING", "").strip():
                    self.state_reason = "Falta configurar USER_SESSION_STRING no Railway"
                    await self.persist_state(False, self.state_reason)
                    return self.state_reason
                await self.persist_state(True, "Conectando")
                self.worker = asyncio.create_task(self.observer_worker())
                return "Observação solicitada. Use /status para conferir."
            # Block new events before waiting for in-flight work to stop.
            self.enabled = False
            try:
                await self.persist_state(False, reason)
            finally:
                await self.stop_worker()
            return "Observação desligada. O painel continua disponível."

    async def persist_state(self, enabled, reason):
        await self.pool.execute("UPDATE observer_control SET enabled=$1, reason=$2, updated_at=NOW() WHERE singleton=TRUE", enabled, reason)
        self.state_reason = reason

    async def stop_worker(self):
        self.enabled = False
        tasks = list(self.active_events)
        if self.worker and self.worker is not asyncio.current_task():
            tasks.append(self.worker)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.worker = None

    async def observer_worker(self):
        tasks = []
        try:
            self.user = TelegramClient(StringSession(required("USER_SESSION_STRING")), self.api_id, self.api_hash, flood_sleep_threshold=0)
            # Incoming messages feed the radar; outgoing messages prove that the
            # account can post in that chat and move it to "Postáveis".
            self.user.add_event_handler(self.guarded_observe, events.NewMessage())
            await self.user.connect()
            if not await self.user.is_user_authorized():
                raise RuntimeError("Sessão não autorizada")
            self.me = await self.user.get_me()
            self.enabled = True
            self.state_reason = "Ligado"
            tasks = [asyncio.create_task(self.scan_loop()),
                     asyncio.create_task(self.user.run_until_disconnected())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            raise RuntimeError("Conexão encerrada")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.enabled = False
            reason = (f"Pausado por FloodWait ({exc.seconds}s); revisar antes de ligar"
                      if isinstance(exc, errors.FloodWaitError)
                      else f"Observação parada: {type(exc).__name__}. Confira a sessão e a conexão.")
            log.warning("%s", reason)
            await self.persist_state(False, reason)
        finally:
            self.enabled = False
            pending = list(self.active_events)
            for task in tasks + pending:
                task.cancel()
            await asyncio.gather(*(tasks + pending), return_exceptions=True)
            if self.user:
                await self.user.disconnect()

    async def guarded_observe(self, event):
        if not self.enabled:
            return
        task = asyncio.current_task()
        self.active_events.add(task)
        try:
            await self.observe_event(event)
        except errors.FloodWaitError as exc:
            self.enabled = False
            reason = f"Pausado por FloodWait ({exc.seconds}s); revisar antes de ligar"
            pause = asyncio.create_task(self.set_enabled(False, reason))
            self.pause_tasks.add(pause)
            pause.add_done_callback(self.pause_tasks.discard)
        finally:
            self.active_events.discard(task)

    async def record_manual_post(self, event):
        """Use a successful outgoing post as direct evidence of permission."""
        if not (event.out and (event.is_group or event.is_channel)):
            return False
        entity = await event.get_chat()
        has_media = bool(getattr(event, "media", None))
        has_link = bool(URL_RE.search(event.raw_text or ""))
        evidence = {"can_text": True, "last_scanned": now()}
        if has_media:
            evidence["can_media"] = True
        if has_link:
            evidence.update(can_links=True, risk="low")
        await self.save_chat(entity, **evidence)
        return True

    async def control_command(self, event):
        if not self.is_admin(event):
            return
        command = event.raw_text.split("@", 1)[0].strip()
        if command == "/ligar":
            text = await self.set_enabled(True)
        elif command == "/desligar":
            text = await self.set_enabled(False)
        else:
            text = self.status_text()
        await event.respond(text, parse_mode=None, link_preview=False)

    def status_text(self):
        status = "LIGADO" if self.enabled else "CONECTANDO" if self.worker and not self.worker.done() else "DESLIGADO"
        return f"Radar: {status}\n{self.state_reason}\nPainel: online"

    def register_handlers(self):
        self.panel.add_event_handler(self.control_command, events.NewMessage(pattern=r"^/(?:ligar|desligar|status)(?:@\w+)?$"))
        async def observe(event):
            if await self.record_manual_post(event):
                return
            sender = await event.get_sender()
            if event.is_group or event.is_channel:
                entity = await event.get_chat()
                await self.save_chat(entity)
                await self.capture_bot(utils.get_peer_id(entity), sender, "new_message")
                await self.capture_links(utils.get_peer_id(entity), event.id, event.raw_text or "")
                if RULE_WORDS.search(event.raw_text or ""):
                    await self.pool.execute(
                        """INSERT INTO visible_rules(chat_id,message_id,excerpt,observed_at)
                        VALUES($1,$2,$3,$4) ON CONFLICT DO NOTHING""",
                        utils.get_peer_id(entity), event.id, (event.raw_text or "")[:600], now())
                await self.detect_directed(event, sender)
            elif event.is_private:
                await self.detect_private_origin(event, sender)

        self.observe_event = observe

        @self.panel.on(events.NewMessage(pattern=r"^/(?:start|observador)(?:@\w+)?$"))
        async def dashboard(event):
            if not self.is_admin(event):
                return
            await self.show_dashboard(event)

        @self.panel.on(events.CallbackQuery)
        async def callback(event):
            if not self.is_admin(event):
                return await event.answer("Sem autorização", alert=True)
            data = event.data.decode()
            if data in {"power:on", "power:off"}:
                result = await self.set_enabled(data == "power:on")
                await event.answer(result[:200], alert=True)
                return await self.show_dashboard(event)
            if data == "home":
                return await self.show_dashboard(event)
            if data in {"groups", "channels", "postable", "uncertain", "bots", "links", "origins", "drafts"}:
                return await self.show_list(event, data, 0)
            detail = re.fullmatch(r"chat:(-?\d+)", data)
            if detail:
                return await self.show_chat(event, int(detail.group(1)))
            match = re.fullmatch(r"list:([a-z]+):(\d+)", data)
            if match:
                return await self.show_list(event, match.group(1), int(match.group(2)))

    async def show_dashboard(self, event):
        counts = await self.pool.fetchrow("""SELECT
          COUNT(*) FILTER(WHERE kind='channel') channels,
          COUNT(*) FILTER(WHERE kind='group') groups,
          COUNT(*) FILTER(WHERE can_text IS TRUE) postable,
          COUNT(*) FILTER(WHERE can_text IS NULL OR risk IN ('unknown','medium')) uncertain
          FROM chats""")
        bots = await self.pool.fetchval("SELECT COUNT(DISTINCT bot_id) FROM observed_bots")
        links = await self.pool.fetchval("SELECT COUNT(*) FROM discovered_links WHERE status='pending'")
        text = (f"🔎 OBSERVADOR\n{self.status_text()}\n\n📢 Canais: {counts['channels']}\n👥 Grupos: {counts['groups']}\n"
                f"✅ Pode postar: {counts['postable']}\n⚠️ Incertos: {counts['uncertain']}\n"
                f"🤖 Bots: {bots}\n🔗 Links aguardando: {links}")
        # One button per row uses the available Telegram width and keeps labels readable.
        buttons = [[Button.inline(f"📢 Canais ({counts['channels']})", b"channels")],
                   [Button.inline(f"👥 Grupos ({counts['groups']})", b"groups")],
                   [Button.inline(f"✅ Postáveis ({counts['postable']})", b"postable")],
                   [Button.inline(f"⚠️ Incertos ({counts['uncertain']})", b"uncertain")],
                   [Button.inline(f"🤖 Bots ({bots})", b"bots")],
                   [Button.inline(f"🔗 Links ({links})", b"links")],
                   [Button.inline("👤 Origens PV", b"origins")],
                   [Button.inline("📝 Rascunhos", b"drafts")],
                   [Button.inline("▶️ Ligar observação", b"power:on")],
                   [Button.inline("⏸️ Desligar observação", b"power:off")]]
        if isinstance(event, events.CallbackQuery.Event):
            await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
        else:
            await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)

    async def show_list(self, event, section, offset):
        limit = 8
        item_buttons = []
        if section == "bots":
            rows = await self.pool.fetch("SELECT COALESCE(username,display_name,'bot') label, COUNT(*) total FROM observed_bots GROUP BY 1 ORDER BY total DESC OFFSET $1 LIMIT $2", offset, limit)
            lines = [f"🤖 @{r['label']} — {r['total']} grupo(s)" for r in rows]
        elif section == "links":
            rows = await self.pool.fetch("""SELECT l.url label, l.chat_id source_id, c.title source_title
                FROM discovered_links l LEFT JOIN chats c ON c.chat_id=l.chat_id
                WHERE l.status='pending' ORDER BY l.observed_at DESC OFFSET $1 LIMIT $2""", offset, limit)
            lines = [f"🔗 {r['label']}\n   origem: {source_label(r['source_title'], r['source_id'])}" for r in rows]
        elif section == "origins":
            rows = await self.pool.fetch("""SELECT COALESCE(p.username,p.user_id::text) label,
                p.source_chat_id source_id, c.title source_title
                FROM private_origins p LEFT JOIN chats c ON c.chat_id=p.source_chat_id
                ORDER BY p.detected_at DESC OFFSET $1 LIMIT $2""", offset, limit)
            lines = [f"👤 @{r['label']} ← {source_label(r['source_title'], r['source_id'])}" for r in rows]
        elif section == "drafts":
            rows = await self.pool.fetch("SELECT reason label, user_id total FROM drafts WHERE status='pending' ORDER BY created_at DESC OFFSET $1 LIMIT $2", offset, limit)
            lines = [f"📝 {r['label']} — usuário {r['total']}" for r in rows]
        else:
            where = {"groups": "kind='group'", "channels": "kind='channel'", "postable": "can_text IS TRUE", "uncertain": "can_text IS NULL OR risk IN ('unknown','medium')"}[section]
            rows = await self.pool.fetch(f"SELECT chat_id, title label, can_text, can_media, can_links FROM chats WHERE {where} ORDER BY title OFFSET $1 LIMIT $2", offset, limit)
            lines = [("🟢" if r['can_text'] else "🔴" if r['can_text'] is False else "⚪") + f" {r['label']} | texto={r['can_text']} mídia={r['can_media']} links={r['can_links']}" for r in rows]
            item_buttons = [[Button.inline(r["label"][:60], f"chat:{r['chat_id']}".encode())] for r in rows]
        nav = []
        if offset:
            nav.append(Button.inline("⬅️", f"list:{section}:{max(0, offset-limit)}".encode()))
        if len(rows) == limit:
            nav.append(Button.inline("➡️", f"list:{section}:{offset+limit}".encode()))
        buttons = item_buttons + ([nav] if nav else [])
        buttons.append([Button.inline("↩️ Início", b"home")])
        await event.edit("\n\n".join(lines) if lines else "Nenhum item encontrado.", buttons=buttons, parse_mode=None, link_preview=False)

    async def show_chat(self, event, chat_id):
        row = await self.pool.fetchrow("SELECT * FROM chats WHERE chat_id=$1", chat_id)
        if not row:
            return await event.answer("Grupo não encontrado")
        label = lambda value: "sim" if value is True else "não" if value is False else "incerto"
        rules = await self.pool.fetch("SELECT excerpt FROM visible_rules WHERE chat_id=$1 ORDER BY observed_at DESC LIMIT 3", chat_id)
        bots = await self.pool.fetch("SELECT COALESCE(username,display_name,'bot') label FROM observed_bots WHERE chat_id=$1 LIMIT 8", chat_id)
        text = (f"{row['title']}\n\nTexto: {label(row['can_text'])}\n"
                f"Mídia (geral): {label(row['can_media'])}\nLinks/divulgação: {label(row['can_links'])}\n"
                f"Slow mode: {row['slowmode_seconds']} s\nÚltima consulta: {row['last_scanned']}\n\n"
                "Trechos candidatos a regras (revisar):\n" + "\n".join(r['excerpt'][:400] for r in rules)
                + "\n\nBots observados: " + ", ".join(r['label'] for r in bots))
        buttons = []
        if row['username']:
            buttons.append([Button.url("Abrir no Telegram", f"https://t.me/{row['username']}")])
        elif chat_id < -1000000000000:
            buttons.append([Button.url("Abrir no Telegram", f"https://t.me/c/{-chat_id-1000000000000}/1")])
        buttons.append([Button.inline("Voltar ao painel", b"home")])
        await event.edit(text[:3800], buttons=buttons, parse_mode=None, link_preview=False)

    async def run(self):
        try:
            await self.setup()
            log.info("Painel Radar GR online; %s", self.state_reason)
            await self.panel.run_until_disconnected()
        finally:
            # Shutdown preserves the administrator's last saved preference.
            await self.stop_worker()
            if self.pause_tasks:
                await asyncio.gather(*list(self.pause_tasks), return_exceptions=True)
            await self.panel.disconnect()
            if self.pool:
                await self.pool.close()


if __name__ == "__main__":
    names = ("TELEGRAM_API_ID", "TELEGRAM_API_HASH",
             "CONTROL_BOT_TOKEN", "ADMIN_USER_ID", "DATABASE_URL")
    missing = [name for name in names if not os.getenv(name, "").strip()]
    if missing:
        log.error("Configuração pendente: %s. Painel não iniciado.", ", ".join(missing))
        raise SystemExit(0)
    asyncio.run(Observer().run())
