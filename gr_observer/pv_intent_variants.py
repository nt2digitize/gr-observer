"""Durable A/B/C/D copy rotation for the linear PV conversation.

The selector is deliberately narrow:
- conversation order remains owned by ``PvLinearRuntimeMixin``;
- membership/context decides only which repertoires are eligible;
- the same ``action_key`` always resolves to the same variant on retry;
- history is per user + intent and never creates Telegram work.
"""

from __future__ import annotations

from dataclasses import dataclass


VARIANT_SLOTS = ("A", "B", "C", "D")

DDL = """
CREATE TABLE IF NOT EXISTS pv_linear_intent_variants (
  id BIGSERIAL PRIMARY KEY,
  step_id BIGINT NOT NULL REFERENCES pv_message_steps(id) ON DELETE CASCADE,
  intent_key TEXT NOT NULL,
  slot TEXT NOT NULL CHECK(slot IN ('A','B','C','D')),
  content TEXT NOT NULL,
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(step_id,intent_key,slot)
);
CREATE INDEX IF NOT EXISTS pv_linear_intent_variants_lookup_idx
ON pv_linear_intent_variants(step_id,intent_key,enabled,slot);

CREATE TABLE IF NOT EXISTS pv_linear_variant_history (
  id BIGSERIAL PRIMARY KEY,
  action_key TEXT NOT NULL UNIQUE,
  user_id BIGINT NOT NULL,
  step_id BIGINT NOT NULL REFERENCES pv_message_steps(id) ON DELETE CASCADE,
  intent_key TEXT NOT NULL,
  slot TEXT NOT NULL CHECK(slot IN ('A','B','C','D')),
  variant_id BIGINT NOT NULL REFERENCES pv_linear_intent_variants(id) ON DELETE RESTRICT,
  selected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pv_linear_variant_history_user_intent_idx
ON pv_linear_variant_history(user_id,intent_key,id DESC);
"""


@dataclass(frozen=True)
class VariantSelection:
    action_key: str
    user_id: int
    step_id: int
    intent_key: str
    slot: str
    variant_id: int
    content: str


def next_variant_slot(available_slots, last_slot: str | None) -> str | None:
    """Rotate A→B→C→D, skipping unavailable slots without random choice."""
    available = tuple(slot for slot in VARIANT_SLOTS if slot in set(available_slots))
    if not available:
        return None
    if last_slot not in VARIANT_SLOTS:
        return available[0]
    start = VARIANT_SLOTS.index(last_slot)
    for offset in range(1, len(VARIANT_SLOTS) + 1):
        candidate = VARIANT_SLOTS[(start + offset) % len(VARIANT_SLOTS)]
        if candidate in available:
            return candidate
    return available[0]


class PvIntentVariantStore:
    """Read/write store for configured variants and retry-stable selection history."""

    def __init__(self, pool):
        self.pool = pool
        self._ready = False

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        await self.pool.execute(DDL)
        self._ready = True

    async def configured(self, step_id: int, intent_key: str):
        await self.ensure_ready()
        return await self.pool.fetch(
            """SELECT id,step_id,intent_key,slot,content
               FROM pv_linear_intent_variants
               WHERE step_id=$1 AND intent_key=$2 AND enabled IS TRUE
               ORDER BY CASE slot
                 WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 WHEN 'D' THEN 4
                 ELSE 99 END,id""",
            int(step_id),
            str(intent_key),
        )

    async def upsert_variant(
        self,
        *,
        step_id: int,
        intent_key: str,
        slot: str,
        content: str,
    ) -> int:
        """Persist operator-configured copy; no runtime state or Telegram effect."""
        await self.ensure_ready()
        normalized_slot = str(slot).strip().upper()
        if normalized_slot not in VARIANT_SLOTS:
            raise ValueError("slot deve ser A, B, C ou D")
        normalized_intent = str(intent_key).strip()
        normalized_content = str(content).strip()
        if not normalized_intent:
            raise ValueError("intent vazio")
        if not normalized_content:
            raise ValueError("variante vazia")
        variant_id = await self.pool.fetchval(
            """INSERT INTO pv_linear_intent_variants(
                 step_id,intent_key,slot,content,enabled,updated_at)
               VALUES($1,$2,$3,$4,TRUE,NOW())
               ON CONFLICT(step_id,intent_key,slot) DO UPDATE SET
                 content=EXCLUDED.content,enabled=TRUE,updated_at=NOW()
               RETURNING id""",
            int(step_id),
            normalized_intent,
            normalized_slot,
            normalized_content,
        )
        return int(variant_id)

    @staticmethod
    def _selection(row) -> VariantSelection:
        return VariantSelection(
            action_key=str(row["action_key"]),
            user_id=int(row["user_id"]),
            step_id=int(row["step_id"]),
            intent_key=str(row["intent_key"]),
            slot=str(row["slot"]),
            variant_id=int(row["variant_id"]),
            content=str(row["content"]),
        )

    async def selection_for_action(self, action_key: str) -> VariantSelection | None:
        await self.ensure_ready()
        row = await self.pool.fetchrow(
            """SELECT h.action_key,h.user_id,h.step_id,h.intent_key,h.slot,
                      h.variant_id,v.content
               FROM pv_linear_variant_history h
               JOIN pv_linear_intent_variants v ON v.id=h.variant_id
               WHERE h.action_key=$1""",
            str(action_key),
        )
        return self._selection(row) if row is not None else None

    async def select_for_action(
        self,
        *,
        action_key: str,
        user_id: int,
        step_id: int,
        intent_keys,
    ) -> VariantSelection | None:
        """Reserve one durable variant for this outbound action.

        Existing history wins first, making retries stable. For a new action the
        first eligible intent with configured copy is selected, then its slot
        advances from the lead's previous slot. The single Writer remains the
        serialization boundary for outbound actions.
        """
        await self.ensure_ready()
        key = str(action_key)
        existing = await self.selection_for_action(key)
        if existing is not None:
            return existing

        normalized_intents = tuple(
            dict.fromkeys(
                str(value).strip()
                for value in intent_keys
                if str(value).strip()
            )
        )
        if not normalized_intents:
            return None

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Re-check inside the transaction in case a retry raced the first read.
                existing_row = await conn.fetchrow(
                    """SELECT h.action_key,h.user_id,h.step_id,h.intent_key,h.slot,
                              h.variant_id,v.content
                       FROM pv_linear_variant_history h
                       JOIN pv_linear_intent_variants v ON v.id=h.variant_id
                       WHERE h.action_key=$1""",
                    key,
                )
                if existing_row is not None:
                    return self._selection(existing_row)

                variants = None
                chosen_intent = None
                for intent_key in normalized_intents:
                    rows = await conn.fetch(
                        """SELECT id,step_id,intent_key,slot,content
                           FROM pv_linear_intent_variants
                           WHERE step_id=$1 AND intent_key=$2 AND enabled IS TRUE
                           ORDER BY CASE slot
                             WHEN 'A' THEN 1 WHEN 'B' THEN 2 WHEN 'C' THEN 3 WHEN 'D' THEN 4
                             ELSE 99 END,id""",
                        int(step_id),
                        intent_key,
                    )
                    if rows:
                        chosen_intent = intent_key
                        variants = list(rows)
                        break
                if not variants or chosen_intent is None:
                    return None

                last_slot = await conn.fetchval(
                    """SELECT slot FROM pv_linear_variant_history
                       WHERE user_id=$1 AND intent_key=$2
                       ORDER BY id DESC LIMIT 1""",
                    int(user_id),
                    chosen_intent,
                )
                chosen_slot = next_variant_slot(
                    [str(row["slot"]) for row in variants],
                    str(last_slot) if last_slot is not None else None,
                )
                if chosen_slot is None:
                    return None
                chosen = next(row for row in variants if str(row["slot"]) == chosen_slot)

                inserted = await conn.fetchrow(
                    """INSERT INTO pv_linear_variant_history(
                         action_key,user_id,step_id,intent_key,slot,variant_id)
                       VALUES($1,$2,$3,$4,$5,$6)
                       ON CONFLICT(action_key) DO NOTHING
                       RETURNING action_key,user_id,step_id,intent_key,slot,variant_id""",
                    key,
                    int(user_id),
                    int(step_id),
                    chosen_intent,
                    chosen_slot,
                    int(chosen["id"]),
                )
                if inserted is None:
                    retry = await conn.fetchrow(
                        """SELECT h.action_key,h.user_id,h.step_id,h.intent_key,h.slot,
                                  h.variant_id,v.content
                           FROM pv_linear_variant_history h
                           JOIN pv_linear_intent_variants v ON v.id=h.variant_id
                           WHERE h.action_key=$1""",
                        key,
                    )
                    return self._selection(retry) if retry is not None else None

                return VariantSelection(
                    action_key=key,
                    user_id=int(user_id),
                    step_id=int(step_id),
                    intent_key=chosen_intent,
                    slot=chosen_slot,
                    variant_id=int(chosen["id"]),
                    content=str(chosen["content"]),
                )
