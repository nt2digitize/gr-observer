"""Passive PV MiniLearn shadow.

This subcomponent never sends Telegram messages. It reduces inbound private text
into a coarse intent label, then learns only text replies that cannot be
attributed to the existing Writer/effect journal. Inbound raw text is never
persisted.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import normalize_text

MINILEARN_SCHEMA = """
CREATE TABLE IF NOT EXISTS pv_minilearn_context (
  user_id BIGINT PRIMARY KEY,
  intent TEXT NOT NULL,
  inbound_message_id BIGINT NOT NULL,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pv_minilearn_context_time_idx
ON pv_minilearn_context(observed_at DESC);

CREATE TABLE IF NOT EXISTS pv_minilearn_memory (
  intent TEXT NOT NULL,
  normalized_response TEXT NOT NULL,
  response_text TEXT NOT NULL,
  times_seen INTEGER NOT NULL DEFAULT 1,
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY(intent,normalized_response)
);
CREATE INDEX IF NOT EXISTS pv_minilearn_memory_strength_idx
ON pv_minilearn_memory(intent,times_seen DESC,last_seen_at DESC);
"""

INTENT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("price", ("quanto custa", "qual o valor", "quanto ta", "quanto tá", "qnt custa", "qnt ta", "qnt tá", "preco", "preço", "valor do vip", "valor vip")),
    ("payment", ("pix", "pagamento", "pagar", "cartao", "cartão")),
    ("access", ("como entra", "como entrar", "como funciona", "manda o link", "qual o link", "link")),
    ("live", ("ao vivo", "live")),
    ("preview", ("previa", "prévia", "amostra")),
    ("new_content", ("conteudo novo", "conteúdo novo", "tem algo novo", "novidade")),
)


def classify_minilearn_intent(text: str) -> str:
    value = normalize_text(text or "")
    for intent, markers in INTENT_RULES:
        if any(marker in value for marker in markers):
            return intent
    return "unknown"


@dataclass(frozen=True)
class MiniLearnObservation:
    intent: str
    normalized_response: str
    learned: bool
    reason: str


class PvResponseMemoryShadow:
    def __init__(self, pool, *, enabled: bool = False) -> None:
        self.pool = pool
        self.enabled = bool(enabled)
        self._ready = False

    async def ensure_schema(self) -> None:
        if not self.enabled or self._ready:
            return
        await self.pool.execute(MINILEARN_SCHEMA)
        self._ready = True

    async def observe_inbound(self, *, user_id: int, message_id: int, text: str) -> str:
        if not self.enabled:
            return "disabled"
        await self.ensure_schema()
        intent = classify_minilearn_intent(text)
        await self.pool.execute(
            """INSERT INTO pv_minilearn_context(user_id,intent,inbound_message_id,observed_at)
               VALUES($1,$2,$3,NOW())
               ON CONFLICT(user_id) DO UPDATE SET
                 intent=EXCLUDED.intent,
                 inbound_message_id=EXCLUDED.inbound_message_id,
                 observed_at=NOW()""",
            int(user_id), intent, int(message_id),
        )
        return intent

    async def _is_writer_effect(self, *, peer: int, message_id: int, text: str) -> bool:
        return bool(await self.pool.fetchval(
            """SELECT EXISTS(
                 SELECT 1 FROM telegram_effects te
                 WHERE te.created_at > NOW()-INTERVAL '5 minutes'
                   AND te.effect_type IN ('send_text','send_text_preview','send_panel_text')
                   AND (
                     (te.result ? 'message_id' AND (te.result->>'message_id')::BIGINT=$2)
                     OR (te.payload->>'peer'=$1::TEXT AND COALESCE(te.payload->>'text','')=$3)
                   )
               )""",
            int(peer), int(message_id), text.strip(),
        ))

    async def observe_outbound(self, *, user_id: int, message_id: int, text: str, has_media: bool) -> MiniLearnObservation:
        if not self.enabled:
            return MiniLearnObservation("disabled", "", False, "disabled")
        if has_media:
            return MiniLearnObservation("ignored", "", False, "media")
        normalized = normalize_text(text or "")
        if not normalized:
            return MiniLearnObservation("ignored", "", False, "empty")
        await self.ensure_schema()
        row = await self.pool.fetchrow(
            "SELECT intent FROM pv_minilearn_context WHERE user_id=$1 AND observed_at > NOW()-INTERVAL '24 hours'",
            int(user_id),
        )
        if row is None:
            return MiniLearnObservation("unknown", normalized, False, "no_context")
        intent = str(row["intent"])
        if intent == "unknown":
            return MiniLearnObservation(intent, normalized, False, "unknown_intent")
        if await self._is_writer_effect(peer=user_id, message_id=message_id, text=text):
            return MiniLearnObservation(intent, normalized, False, "automation")
        await self.pool.execute(
            """INSERT INTO pv_minilearn_memory(intent,normalized_response,response_text,times_seen,first_seen_at,last_seen_at)
               VALUES($1,$2,$3,1,NOW(),NOW())
               ON CONFLICT(intent,normalized_response) DO UPDATE SET
                 times_seen=pv_minilearn_memory.times_seen+1,
                 response_text=EXCLUDED.response_text,
                 last_seen_at=NOW()""",
            intent, normalized, text.strip(),
        )
        return MiniLearnObservation(intent, normalized, True, "human")
