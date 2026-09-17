"""Single-line PV conversation engine.

Frames/phases are operator context only. Every enabled balloon belongs to one
ordered conversation and decides whether the next balloon is automatic or waits
for one inbound human reply. The engine stays inside the existing pv_reply rib,
Outbox and single Writer.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from decimal import Decimal

from .human_timing import MIN_WRITING_DELAY_SECONDS
from .pv_message_steps import POSITION_GAP, infer_kind
from .pv_suppression import is_suppressed
from .modules.pv_reply import display_name, is_human_sender

LINEAR_MIGRATION_VERSION = 4
LINEAR_POSITION_GAP = Decimal("1000000")

PHASE_DEFAULTS = (
    (
        "entry",
        Decimal("1000000"),
        "Entrada / Saudação",
        "Iniciar a conversa e gerar abertura para o lead responder.",
    ),
    (
        "preview",
        Decimal("2000000"),
        "Acesso à prévia",
        "Conduzir naturalmente até a prévia ou outro destino configurado.",
    ),
    (
        "warmup",
        Decimal("3000000"),
        "Pós-prévia / Aquecimento",
        "Entender se entrou, se gostou e continuar a conversa sem reiniciar o funil.",
    ),
    (
        "remarketing",
        Decimal("4000000"),
        "Remarketing",
        "Retomar a conversa quando a cadência pedir uma nova aproximação.",
    ),
)

BLOCK_TO_PHASE = {
    "greeting": "entry",
    "link": "preview",
    "followup": "warmup",
    "weekly": "remarketing",
}

LINEAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS pv_conversation_phases (
    phase_key TEXT PRIMARY KEY,
    position NUMERIC(40,10) NOT NULL,
    label TEXT NOT NULL,
    instruction TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
ALTER TABLE pv_message_steps ADD COLUMN IF NOT EXISTS phase_key TEXT;
ALTER TABLE pv_message_steps ADD COLUMN IF NOT EXISTS linear_position NUMERIC(40,10);
ALTER TABLE pv_message_steps ADD COLUMN IF NOT EXISTS wait_for_reply BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE pv_message_steps ADD COLUMN IF NOT EXISTS linear_enabled BOOLEAN NOT NULL DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS pv_message_steps_linear_idx
ON pv_message_steps(linear_enabled,linear_position,id);
CREATE TABLE IF NOT EXISTS pv_linear_sessions (
    user_id BIGINT PRIMARY KEY,
    status TEXT NOT NULL CHECK(status IN ('active','waiting_reply','completed','stopped')),
    current_step_id BIGINT NULL REFERENCES pv_message_steps(id) ON DELETE SET NULL,
    current_position NUMERIC(40,10),
    generation BIGINT NOT NULL DEFAULT 1,
    last_inbound_message_id BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def linear_flow_enabled() -> bool:
    return os.getenv("PV_LINEAR_FLOW_ENABLED", "0").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
        "sim",
    }


class PvLinearConversationStore:
    def __init__(self, pool):
        self.pool = pool
        self._ready = False

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(LINEAR_SCHEMA)
                applied = await conn.fetchval(
                    "SELECT 1 FROM pv_message_step_migrations WHERE version=$1",
                    LINEAR_MIGRATION_VERSION,
                )
                for phase_key, position, label, instruction in PHASE_DEFAULTS:
                    await conn.execute(
                        """INSERT INTO pv_conversation_phases(
                             phase_key,position,label,instruction)
                           VALUES($1,$2,$3,$4)
                           ON CONFLICT(phase_key) DO NOTHING""",
                        phase_key,
                        position,
                        label,
                        instruction,
                    )
                if not applied:
                    greeting_branch = await conn.fetchval(
                        """SELECT branch_key FROM pv_message_steps
                           WHERE block_key='greeting' AND variant_root=TRUE
                           ORDER BY id LIMIT 1"""
                    )
                    rows = await conn.fetch(
                        """SELECT id,block_key,branch_key,position
                           FROM pv_message_steps
                           WHERE (
                               block_key='greeting' AND branch_key=$1
                           ) OR (
                               block_key IN ('link','followup','weekly')
                               AND branch_key IS NULL
                           )""",
                        greeting_branch,
                    )
                    block_order = {"greeting": 0, "link": 1, "followup": 2, "weekly": 3}
                    ordered = sorted(
                        rows,
                        key=lambda row: (
                            block_order.get(str(row["block_key"]), 99),
                            Decimal(row["position"]),
                            int(row["id"]),
                        ),
                    )
                    for index, row in enumerate(ordered, start=1):
                        block_key = str(row["block_key"])
                        await conn.execute(
                            """UPDATE pv_message_steps
                               SET phase_key=$2,linear_position=$3,
                                   linear_enabled=TRUE,updated_at=NOW()
                               WHERE id=$1""",
                            int(row["id"]),
                            BLOCK_TO_PHASE[block_key],
                            LINEAR_POSITION_GAP * index,
                        )
                    await conn.execute(
                        """INSERT INTO pv_message_step_migrations(version)
                           VALUES($1) ON CONFLICT(version) DO NOTHING""",
                        LINEAR_MIGRATION_VERSION,
                    )
        self._ready = True

    async def phases(self):
        await self.ensure_ready()
        return await self.pool.fetch(
            "SELECT * FROM pv_conversation_phases ORDER BY position,phase_key"
        )

    async def steps(self):
        await self.ensure_ready()
        return await self.pool.fetch(
            """SELECT * FROM pv_message_steps
               WHERE linear_enabled IS TRUE
               ORDER BY linear_position NULLS LAST,id"""
        )

    async def get_step(self, step_id: int):
        await self.ensure_ready()
        return await self.pool.fetchrow(
            "SELECT * FROM pv_message_steps WHERE id=$1", int(step_id)
        )

    async def first_step(self):
        await self.ensure_ready()
        return await self.pool.fetchrow(
            """SELECT * FROM pv_message_steps
               WHERE linear_enabled IS TRUE
               ORDER BY linear_position NULLS LAST,id LIMIT 1"""
        )

    async def next_step(self, after_position):
        await self.ensure_ready()
        if after_position is None:
            return await self.first_step()
        return await self.pool.fetchrow(
            """SELECT * FROM pv_message_steps
               WHERE linear_enabled IS TRUE AND linear_position>$1
               ORDER BY linear_position,id LIMIT 1""",
            Decimal(str(after_position)),
        )

    async def set_wait_for_reply(self, step_id: int, enabled: bool) -> bool:
        await self.ensure_ready()
        changed = await self.pool.fetchval(
            """UPDATE pv_message_steps SET wait_for_reply=$2,updated_at=NOW()
               WHERE id=$1 AND linear_enabled IS TRUE RETURNING id""",
            int(step_id),
            bool(enabled),
        )
        return changed is not None

    async def set_phase(self, step_id: int, phase_key: str) -> bool:
        await self.ensure_ready()
        exists = await self.pool.fetchval(
            "SELECT 1 FROM pv_conversation_phases WHERE phase_key=$1", phase_key
        )
        if not exists:
            return False
        changed = await self.pool.fetchval(
            """UPDATE pv_message_steps SET phase_key=$2,updated_at=NOW()
               WHERE id=$1 AND linear_enabled IS TRUE RETURNING id""",
            int(step_id),
            phase_key,
        )
        return changed is not None

    async def move(self, step_id: int, direction: int) -> bool:
        await self.ensure_ready()
        direction = -1 if int(direction) < 0 else 1
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                current = await conn.fetchrow(
                    """SELECT id,linear_position FROM pv_message_steps
                       WHERE id=$1 AND linear_enabled IS TRUE FOR UPDATE""",
                    int(step_id),
                )
                if not current or current["linear_position"] is None:
                    return False
                operator = "<" if direction < 0 else ">"
                ordering = "DESC" if direction < 0 else "ASC"
                neighbor = await conn.fetchrow(
                    f"""SELECT id,linear_position FROM pv_message_steps
                        WHERE linear_enabled IS TRUE
                          AND linear_position {operator} $1
                        ORDER BY linear_position {ordering},id {ordering} LIMIT 1
                        FOR UPDATE""",
                    current["linear_position"],
                )
                if not neighbor:
                    return False
                await conn.execute(
                    "UPDATE pv_message_steps SET linear_position=$2,updated_at=NOW() WHERE id=$1",
                    int(current["id"]),
                    neighbor["linear_position"],
                )
                await conn.execute(
                    "UPDATE pv_message_steps SET linear_position=$2,updated_at=NOW() WHERE id=$1",
                    int(neighbor["id"]),
                    current["linear_position"],
                )
                return True

    async def remove_from_line(self, step_id: int) -> bool:
        await self.ensure_ready()
        changed = await self.pool.fetchval(
            """UPDATE pv_message_steps
               SET linear_enabled=FALSE,updated_at=NOW()
               WHERE id=$1 AND linear_enabled IS TRUE RETURNING id""",
            int(step_id),
        )
        return changed is not None

    async def add_phase(self, label: str, instruction: str):
        await self.ensure_ready()
        clean_label = (label or "").strip()
        if not clean_label:
            raise ValueError("quadro sem nome")
        last = await self.pool.fetchval(
            "SELECT MAX(position) FROM pv_conversation_phases"
        )
        position = Decimal(last) + LINEAR_POSITION_GAP if last is not None else LINEAR_POSITION_GAP
        phase_key = f"custom_{uuid.uuid4().hex[:12]}"
        return await self.pool.fetchrow(
            """INSERT INTO pv_conversation_phases(
                 phase_key,position,label,instruction)
               VALUES($1,$2,$3,$4) RETURNING *""",
            phase_key,
            position,
            clean_label,
            (instruction or "").strip(),
        )

    async def add_after(
        self,
        after_step_id: int | None,
        *,
        content: str,
        median_delay_seconds: int,
        phase_key: str | None = None,
        media_source_peer: int | None = None,
        media_source_message_id: int | None = None,
        media_kind: str | None = None,
    ):
        await self.ensure_ready()
        value = (content or "").strip()
        has_media = media_source_peer is not None and media_source_message_id is not None
        if not value and not has_media:
            raise ValueError("balão vazio")
        median = int(median_delay_seconds)
        if median < MIN_WRITING_DELAY_SECONDS:
            raise ValueError("tempo abaixo do mínimo humano")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                parent = None
                if after_step_id:
                    parent = await conn.fetchrow(
                        """SELECT * FROM pv_message_steps
                           WHERE id=$1 AND linear_enabled IS TRUE FOR UPDATE""",
                        int(after_step_id),
                    )
                if parent is None:
                    last = await conn.fetchrow(
                        """SELECT * FROM pv_message_steps
                           WHERE linear_enabled IS TRUE
                           ORDER BY linear_position DESC NULLS LAST,id DESC LIMIT 1
                           FOR UPDATE"""
                    )
                    parent = last
                if parent is None:
                    first_phase = await conn.fetchrow(
                        "SELECT * FROM pv_conversation_phases ORDER BY position LIMIT 1"
                    )
                    if first_phase is None:
                        raise ValueError("nenhum quadro disponível")
                    chosen_phase = phase_key or str(first_phase["phase_key"])
                    linear_position = LINEAR_POSITION_GAP
                else:
                    chosen_phase = phase_key or str(parent["phase_key"] or "entry")
                    nxt = await conn.fetchval(
                        """SELECT linear_position FROM pv_message_steps
                           WHERE linear_enabled IS TRUE AND linear_position>$1
                           ORDER BY linear_position,id LIMIT 1""",
                        parent["linear_position"],
                    )
                    parent_pos = Decimal(parent["linear_position"])
                    next_pos = Decimal(nxt) if nxt is not None else parent_pos + LINEAR_POSITION_GAP
                    linear_position = (parent_pos + next_pos) / 2
                phase_exists = await conn.fetchval(
                    "SELECT 1 FROM pv_conversation_phases WHERE phase_key=$1",
                    chosen_phase,
                )
                if not phase_exists:
                    raise ValueError("quadro inexistente")
                step_key = f"linear.{uuid.uuid4().hex}"
                return await conn.fetchrow(
                    """INSERT INTO pv_message_steps(
                         step_key,block_key,branch_key,position,label,content,kind,
                         median_delay_seconds,variant_root,built_in,
                         media_source_peer,media_source_message_id,media_kind,
                         phase_key,linear_position,wait_for_reply,linear_enabled)
                       VALUES($1,'linear',NULL,$2,'Novo balão',$3,$4,$5,FALSE,FALSE,
                         $6,$7,$8,$9,$2,FALSE,TRUE)
                       RETURNING *""",
                    step_key,
                    linear_position,
                    value,
                    infer_kind(value),
                    median,
                    int(media_source_peer) if media_source_peer is not None else None,
                    int(media_source_message_id) if media_source_message_id is not None else None,
                    str(media_kind) if media_kind is not None else None,
                    chosen_phase,
                )


class PvLinearRuntimeMixin:
    """Runtime switch for the single-line conversation inside the existing PV rib."""

    def __init__(self, storage, settings):
        self.linear_store = PvLinearConversationStore(storage.pool)
        super().__init__(storage, settings)

    async def on_connect(self, client, me) -> None:
        await super().on_connect(client, me)
        await self.linear_store.ensure_ready()

    def register_actions(self, writer) -> None:
        super().register_actions(writer)
        writer.register(
            self.module_id,
            "send_linear_balloon",
            self.action_send_linear_balloon,
        )

    async def handle_event(self, event) -> bool:
        if not linear_flow_enabled():
            return await super().handle_event(event)
        if not event.is_private or event.out or not event.sender_id:
            return await super().handle_event(event)
        sender = await event.get_sender()
        if not is_human_sender(sender):
            return False
        peer = int(sender.id)
        if self.me is not None and peer == int(self.me.id):
            return False
        if await is_suppressed(self.storage.pool, peer):
            return False

        if getattr(self, "auto_save_contacts", False) and hasattr(self, "contact_ledger"):
            await self.contact_ledger.queue_save(
                module_id=self.module_id,
                user_id=peer,
                username=getattr(sender, "username", None),
                display_name=display_name(sender),
                first_name=getattr(sender, "first_name", None),
                last_name=getattr(sender, "last_name", None),
                source_type="pv",
                source_chat_id=None,
                source_message_id=int(event.id),
                event_key=self._event_key(event),
                already_contact=bool(getattr(sender, "contact", False)),
            )

        await self._linear_accept_inbound(peer, int(event.id))
        return False

    async def _linear_accept_inbound(self, peer: int, message_id: int) -> None:
        await self.linear_store.ensure_ready()
        session = await self.storage.pool.fetchrow(
            "SELECT * FROM pv_linear_sessions WHERE user_id=$1", int(peer)
        )
        if session is None:
            first = await self.linear_store.first_step()
            if first is None:
                return
            generation = 1
            await self.storage.pool.execute(
                """INSERT INTO pv_linear_sessions(
                     user_id,status,current_step_id,current_position,generation,
                     last_inbound_message_id,updated_at)
                   VALUES($1,'active',$2,$3,$4,$5,NOW())
                   ON CONFLICT(user_id) DO NOTHING""",
                int(peer),
                int(first["id"]),
                first["linear_position"],
                generation,
                int(message_id),
            )
            await self._queue_linear_step(peer, first, generation)
            return

        await self.storage.pool.execute(
            """UPDATE pv_linear_sessions SET last_inbound_message_id=$2,updated_at=NOW()
               WHERE user_id=$1""",
            int(peer),
            int(message_id),
        )
        if str(session["status"]) != "waiting_reply":
            return
        nxt = await self.linear_store.next_step(session["current_position"])
        if nxt is None:
            await self.storage.pool.execute(
                """UPDATE pv_linear_sessions SET status='completed',updated_at=NOW()
                   WHERE user_id=$1""",
                int(peer),
            )
            return
        generation = int(session["generation"])
        await self.storage.pool.execute(
            """UPDATE pv_linear_sessions
               SET status='active',current_step_id=$2,current_position=$3,updated_at=NOW()
               WHERE user_id=$1""",
            int(peer),
            int(nxt["id"]),
            nxt["linear_position"],
        )
        await self._queue_linear_step(peer, nxt, generation)

    async def _queue_linear_step(self, peer: int, row, generation: int) -> bool:
        text = self.message_store.render(
            str(row["content"] or ""),
            preview_link=self.settings.pv_preview_link,
        )
        origin_key = f"pv_reply:linear:{int(peer)}:{int(generation)}:{int(row['id'])}"
        _, prewait, _ = self._human_plan(row, origin_key, text)
        action_key = origin_key
        inserted = await self.storage.pool.fetchval(
            """INSERT INTO outbox_actions(
                 action_key,module_id,action_type,payload,available_at)
               VALUES($1,'pv_reply','send_linear_balloon',$2::jsonb,
                 NOW()+($3::double precision*INTERVAL '1 second'))
               ON CONFLICT(action_key) DO NOTHING RETURNING id""",
            action_key,
            _json(
                {
                    "peer": int(peer),
                    "step_id": int(row["id"]),
                    "generation": int(generation),
                }
            ),
            float(max(0, math.ceil(prewait))),
        )
        return inserted is not None

    async def action_send_linear_balloon(self, action: dict, effects) -> dict:
        if not linear_flow_enabled():
            return {"sent": False, "reason": "linear_flow_disabled"}
        payload = action.get("payload") or {}
        peer = int(payload.get("peer") or 0)
        step_id = int(payload.get("step_id") or 0)
        generation = int(payload.get("generation") or 0)
        if peer <= 0 or step_id <= 0:
            return {"sent": False, "reason": "invalid_payload"}
        if await is_suppressed(self.storage.pool, peer):
            return {"sent": False, "reason": "suppressed"}
        session = await self.storage.pool.fetchrow(
            "SELECT * FROM pv_linear_sessions WHERE user_id=$1", peer
        )
        if (
            session is None
            or str(session["status"]) != "active"
            or int(session["generation"]) != generation
            or int(session["current_step_id"] or 0) != step_id
        ):
            return {"sent": False, "reason": "conversation_state_changed"}
        row = await self.linear_store.get_step(step_id)
        if row is None or not bool(row["linear_enabled"]):
            return await self._linear_advance_without_send(peer, session, row)
        sent = await self._send_row(
            effects=effects,
            peer=peer,
            row=row,
            origin_key=str(action["action_key"]),
            variables={},
        )
        if bool(row["wait_for_reply"]):
            await self.storage.pool.execute(
                """UPDATE pv_linear_sessions
                   SET status='waiting_reply',current_step_id=$2,current_position=$3,
                       updated_at=NOW() WHERE user_id=$1""",
                peer,
                step_id,
                row["linear_position"],
            )
            return {**sent, "waiting_reply": True}
        nxt = await self.linear_store.next_step(row["linear_position"])
        if nxt is None:
            await self.storage.pool.execute(
                """UPDATE pv_linear_sessions SET status='completed',updated_at=NOW()
                   WHERE user_id=$1""",
                peer,
            )
            return {**sent, "completed": True}
        await self.storage.pool.execute(
            """UPDATE pv_linear_sessions
               SET status='active',current_step_id=$2,current_position=$3,updated_at=NOW()
               WHERE user_id=$1""",
            peer,
            int(nxt["id"]),
            nxt["linear_position"],
        )
        queued = await self._queue_linear_step(peer, nxt, generation)
        return {**sent, "queued_next": queued, "next_step_id": int(nxt["id"])}

    async def _linear_advance_without_send(self, peer: int, session, row) -> dict:
        position = session["current_position"] if row is None else row["linear_position"]
        nxt = await self.linear_store.next_step(position)
        if nxt is None:
            await self.storage.pool.execute(
                """UPDATE pv_linear_sessions SET status='completed',updated_at=NOW()
                   WHERE user_id=$1""",
                peer,
            )
            return {"sent": False, "reason": "step_removed", "completed": True}
        await self.storage.pool.execute(
            """UPDATE pv_linear_sessions
               SET status='active',current_step_id=$2,current_position=$3,updated_at=NOW()
               WHERE user_id=$1""",
            peer,
            int(nxt["id"]),
            nxt["linear_position"],
        )
        queued = await self._queue_linear_step(peer, nxt, int(session["generation"]))
        return {"sent": False, "reason": "step_removed", "queued_next": queued}
