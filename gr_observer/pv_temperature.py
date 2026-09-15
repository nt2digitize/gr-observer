"""Shadow-only PV temperature and vacuum diagnostics.

This module never sends Telegram messages and never changes Outbox priority. It
only classifies inbound private demand using facts already persisted by the
monolith, then records an auditable observation without storing raw chat text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .catalog import normalize_text

TEMPERATURE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pv_temperature_observations (
  id BIGSERIAL PRIMARY KEY,
  event_key TEXT NOT NULL UNIQUE,
  user_id BIGINT NOT NULL,
  demand_signal TEXT NOT NULL,
  score INTEGER NOT NULL,
  band TEXT NOT NULL CHECK(band IN ('cold','cool','warm','hot','excluded')),
  vacuum_candidate BOOLEAN NOT NULL DEFAULT FALSE,
  stage TEXT,
  source_chat_id BIGINT,
  pending_actions INTEGER NOT NULL DEFAULT 0,
  ready_actions INTEGER NOT NULL DEFAULT 0,
  reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
  observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pv_temperature_band_time_idx
ON pv_temperature_observations(band,observed_at DESC);
CREATE INDEX IF NOT EXISTS pv_temperature_user_time_idx
ON pv_temperature_observations(user_id,observed_at DESC);
"""


@dataclass(frozen=True)
class TemperatureDecision:
    score: int
    band: str
    vacuum_candidate: bool
    reasons: tuple[str, ...]


def classify_demand_signal(text: str) -> str:
    """Return only a coarse intent label; raw inbound text is never persisted."""
    value = normalize_text(text or "")
    if "link" not in value:
        return "none"
    request_terms = (
        "manda", "mandar", "passa", "passar", "quero",
        "cade", "cadê", "qual", "tem", "envia", "enviar", "me da", "me dá",
    )
    if any(term in value for term in request_terms):
        return "explicit_link_request"
    return "link_mentioned"


def decide_temperature(
    *,
    demand_signal: str,
    inbound_age_seconds: float | None,
    recent_group_interaction: bool,
    probable_group_origin: bool,
    pending_actions: int,
    ready_actions: int,
    stage: str | None,
    suppressed: bool,
) -> TemperatureDecision:
    reasons: list[str] = []
    if suppressed or stage == "stopped":
        reasons.append("automation_excluded")
        return TemperatureDecision(0, "excluded", False, tuple(reasons))

    score = 0
    if demand_signal == "explicit_link_request":
        score += 45
        reasons.append("explicit_link_request")
    elif demand_signal == "link_mentioned":
        score += 12
        reasons.append("link_mentioned")

    age = None if inbound_age_seconds is None else max(0.0, float(inbound_age_seconds))
    if age is not None and age <= 15 * 60:
        score += 20
        reasons.append("pv_recent_15m")
    elif age is not None and age <= 2 * 3600:
        score += 15
        reasons.append("pv_recent_2h")
    elif age is not None and age <= 24 * 3600:
        score += 8
        reasons.append("pv_recent_24h")

    if recent_group_interaction:
        score += 20
        reasons.append("recent_group_interaction")
    elif probable_group_origin:
        score += 15
        reasons.append("probable_group_origin")

    ready = max(0, int(ready_actions))
    pending = max(0, int(pending_actions))
    if ready:
        score += 20
        reasons.append("pv_work_ready")
    elif pending:
        score += 10
        reasons.append("pv_work_pending")

    vacuum = bool(
        demand_signal == "explicit_link_request"
        and age is not None
        and age <= 24 * 3600
        and pending > 0
    )
    if vacuum:
        reasons.append("vacuum_candidate")

    if score >= 70:
        band = "hot"
    elif score >= 40:
        band = "warm"
    elif score >= 20:
        band = "cool"
    else:
        band = "cold"
    return TemperatureDecision(score, band, vacuum, tuple(reasons))


class PvTemperatureShadow:
    """Event-driven shadow observer using only existing persisted evidence."""

    def __init__(self, pool) -> None:
        self.pool = pool

    async def ensure_schema(self) -> None:
        await self.pool.execute(TEMPERATURE_SCHEMA)

    async def observe(self, *, event_key: str, user_id: int, demand_signal: str) -> TemperatureDecision | None:
        row = await self.pool.fetchrow(
            """SELECT
                 c.stage,
                 EXTRACT(EPOCH FROM (NOW()-c.last_inbound_at))::DOUBLE PRECISION AS inbound_age_seconds,
                 (
                   SELECT po.source_chat_id FROM private_origins po
                   WHERE po.user_id=c.user_id
                     AND po.detected_at > NOW()-INTERVAL '24 hours'
                   ORDER BY po.detected_at DESC LIMIT 1
                 ) AS source_chat_id,
                 EXISTS(
                   SELECT 1 FROM group_interactions gi
                   WHERE gi.user_id=c.user_id
                     AND gi.observed_at > NOW()-INTERVAL '24 hours'
                 ) AS recent_group_interaction,
                 (
                   SELECT COUNT(*)::INTEGER FROM outbox_actions oa
                   WHERE oa.module_id='pv_reply'
                     AND oa.status IN ('pending','processing')
                     AND oa.payload ? 'peer'
                     AND oa.payload->>'peer'=c.user_id::text
                     AND oa.action_type LIKE 'send_%'
                 ) AS pending_actions,
                 (
                   SELECT COUNT(*)::INTEGER FROM outbox_actions oa
                   WHERE oa.module_id='pv_reply'
                     AND (
                       (oa.status='pending' AND oa.available_at<=NOW())
                       OR oa.status='processing'
                     )
                     AND oa.payload ? 'peer'
                     AND oa.payload->>'peer'=c.user_id::text
                     AND oa.action_type LIKE 'send_%'
                 ) AS ready_actions,
                 EXISTS(
                   SELECT 1 FROM pv_suppressed_users ps
                   WHERE ps.user_id=c.user_id AND ps.active IS TRUE
                 ) AS suppressed
               FROM pv_reply_contacts c
               WHERE c.user_id=$1""",
            int(user_id),
        )
        if row is None:
            return None

        source_chat_id = row["source_chat_id"]
        decision = decide_temperature(
            demand_signal=demand_signal,
            inbound_age_seconds=row["inbound_age_seconds"],
            recent_group_interaction=bool(row["recent_group_interaction"]),
            probable_group_origin=source_chat_id is not None,
            pending_actions=int(row["pending_actions"] or 0),
            ready_actions=int(row["ready_actions"] or 0),
            stage=str(row["stage"]) if row["stage"] is not None else None,
            suppressed=bool(row["suppressed"]),
        )
        await self.pool.execute(
            """INSERT INTO pv_temperature_observations(
                 event_key,user_id,demand_signal,score,band,vacuum_candidate,
                 stage,source_chat_id,pending_actions,ready_actions,reasons
               ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11::jsonb)
               ON CONFLICT(event_key) DO NOTHING""",
            str(event_key),
            int(user_id),
            demand_signal,
            decision.score,
            decision.band,
            decision.vacuum_candidate,
            str(row["stage"]) if row["stage"] is not None else None,
            int(source_chat_id) if source_chat_id is not None else None,
            int(row["pending_actions"] or 0),
            int(row["ready_actions"] or 0),
            json.dumps(list(decision.reasons), ensure_ascii=False),
        )
        return decision
