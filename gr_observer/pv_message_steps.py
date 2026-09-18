"""Configurable copy layer for the existing PV journey.

This is not another rib. It stores only operator-editable message copy/order/timing;
the established pv_reply state machine remains the owner of triggers and business state.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlparse

from .catalog import (
    CAMPAIGNS,
    DIALOGS,
    LIVE_INVITE_VARIANTS,
    LIVE_REMARKETING_VARIANTS,
    PV_GREETING_VARIANTS,
)
from .pv_photo_flow import caption_for_slot

SEED_VERSION = 1
POSITION_GAP = Decimal("1000000")
MAX_MEDIAN_SECONDS = 365 * 24 * 3600
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_TRAILING_NUMBER_RE = re.compile(r"(\d+)$")

DDL = """
CREATE TABLE IF NOT EXISTS pv_message_step_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS pv_message_steps (
    id BIGSERIAL PRIMARY KEY,
    step_key TEXT NOT NULL UNIQUE,
    block_key TEXT NOT NULL,
    branch_key TEXT NULL,
    position NUMERIC(40,10) NOT NULL,
    label TEXT NOT NULL,
    content TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('text','link')),
    median_delay_seconds INTEGER NOT NULL CHECK(median_delay_seconds >= 0),
    variant_root BOOLEAN NOT NULL DEFAULT FALSE,
    built_in BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pv_message_steps_order_idx
ON pv_message_steps(block_key, branch_key, position, id);
ALTER TABLE pv_message_steps
ADD COLUMN IF NOT EXISTS media_source_peer BIGINT;
ALTER TABLE pv_message_steps
ADD COLUMN IF NOT EXISTS media_source_message_id BIGINT;
ALTER TABLE pv_message_steps
ADD COLUMN IF NOT EXISTS media_kind TEXT;
"""

BLOCK_ORDER = (
    "greeting",
    "link",
    "followup",
    "weekly",
    "live_optin",
    "live_invite",
    "live_remarketing",
    "live_link",
    "two_screens_prompt",
    "two_screens_preference",
    "two_screens_question",
    "two_screens_limit",
    "two_screens_followup",
    "two_screens_retry_preference",
    "two_screens_retry",
    "photo_caption_peitos",
    "photo_caption_buceta",
    "photo_caption_cu",
)
BLOCK_LABELS = {
    "greeting": "Abertura",
    "link": "Convite e prévia",
    "followup": "Follow-up",
    "weekly": "Sequência semanal",
    "live_optin": "Opt-in de live",
    "live_invite": "Convite de live",
    "live_remarketing": "Remarketing de live",
    "live_link": "Link da live",
    "two_screens_prompt": "Duas telas — abertura",
    "two_screens_preference": "Duas telas — preferência ativa",
    "two_screens_question": "Duas telas — pergunta legada",
    "two_screens_limit": "Duas telas — limite",
    "two_screens_followup": "Duas telas — follow-up",
    "two_screens_retry_preference": "Duas telas — retry ativo",
    "two_screens_retry": "Duas telas — retry legado",
    "photo_caption_peitos": "Legenda foto — peitos",
    "photo_caption_buceta": "Legenda foto — buceta",
    "photo_caption_cu": "Legenda foto — cu",
}


@dataclass(frozen=True)
class DelayPlan:
    median: int
    minimum: int
    maximum: int
    chosen: int


def infer_kind(content: str) -> str:
    value = (content or "").strip()
    if value in {"{preview_link}", "{live_link}"}:
        return "link"
    parsed = urlparse(value)
    if (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and not any(ch.isspace() for ch in value)
    ):
        return "link"
    return "text"


def has_link(content: str) -> bool:
    return infer_kind(content) == "link" or bool(_URL_RE.search(content or ""))


def human_jitter_bounds(median_seconds: int) -> tuple[int, int]:
    """Scale-aware symmetric jitter; median remains the distribution center."""
    median = max(0, int(median_seconds))
    if median == 0:
        return 0, 0
    if median <= 10:
        spread = 1
    elif median <= 60:
        spread = min(4, max(1, round(median * 0.08)))
    elif median <= 300:
        spread = min(15, max(3, round(median * 0.06)))
    elif median <= 3600:
        spread = min(60, max(10, round(median * 0.04)))
    else:
        spread = min(600, max(60, round(median * 0.025)))
    return max(0, median - spread), median + spread


def stable_delay_seconds(key: str, median_seconds: int) -> int:
    low, high = human_jitter_bounds(median_seconds)
    if low == high:
        return low
    width = high - low + 1
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return low + (int.from_bytes(digest[:8], "big") % width)


def delay_plan(key: str, median_seconds: int) -> DelayPlan:
    low, high = human_jitter_bounds(median_seconds)
    return DelayPlan(
        int(median_seconds),
        low,
        high,
        stable_delay_seconds(key, median_seconds),
    )


def typing_seconds(text: str, key: str, *, cap: float = 8.0) -> float:
    """Retry-stable human typing window based on visible character count."""
    visible = len((text or "").strip())
    if visible <= 0:
        return 0.0
    digest = hashlib.sha256(f"typing:{key}".encode("utf-8")).digest()
    cps = 3.5 + ((int.from_bytes(digest[:2], "big") % 76) / 100.0)
    seconds = visible / cps
    return round(max(0.8, min(float(cap), seconds)), 2)


def pre_send_wait_seconds(
    total_delay: int, text: str, key: str
) -> tuple[float, float]:
    typing = min(float(max(0, total_delay)), typing_seconds(text, key))
    return max(0.0, float(total_delay) - typing), typing


def _baseline(settings) -> list[dict]:
    reply_delay = max(0, int(settings.pv_reply_delay_seconds))
    followup_median = round(
        (
            (settings.pv_followup_min_hours + settings.pv_followup_max_hours)
            / 2.0
        )
        * 3600
    )
    weekly = round(settings.pv_weekly_interval_hours * 3600)
    rows: list[dict] = []
    for index, text in enumerate(PV_GREETING_VARIANTS):
        rows.append(
            dict(
                step_key=f"greeting.v{index + 1}",
                block_key="greeting",
                branch_key=f"greeting:v{index + 1}",
                position=POSITION_GAP,
                label=f"Abertura {index + 1}",
                content=text,
                median=reply_delay,
                variant_root=True,
            )
        )
    rows += [
        dict(
            step_key="link.invite",
            block_key="link",
            branch_key=None,
            position=POSITION_GAP,
            label="Convite para prévia",
            content=CAMPAIGNS["pv.link_invite"]["text"],
            median=reply_delay,
        ),
        dict(
            step_key="link.preview",
            block_key="link",
            branch_key=None,
            position=POSITION_GAP * 2,
            label="Link da prévia",
            content="{preview_link}",
            median=7,
        ),
        dict(
            step_key="followup.text",
            block_key="followup",
            branch_key=None,
            position=POSITION_GAP,
            label="Follow-up",
            content=CAMPAIGNS["pv.followup"]["text"],
            median=followup_median,
        ),
        dict(
            step_key="followup.link",
            block_key="followup",
            branch_key=None,
            position=POSITION_GAP * 2,
            label="Link do follow-up",
            content="{preview_link}",
            median=3,
        ),
        dict(
            step_key="weekly.question",
            block_key="weekly",
            branch_key=None,
            position=POSITION_GAP,
            label="Pergunta semanal",
            content=CAMPAIGNS["pv.weekly_question"]["text"],
            median=weekly,
        ),
        dict(
            step_key="weekly.link1",
            block_key="weekly",
            branch_key=None,
            position=POSITION_GAP * 2,
            label="Link semanal 1",
            content="{preview_link}",
            median=7,
        ),
        dict(
            step_key="weekly.reentry",
            block_key="weekly",
            branch_key=None,
            position=POSITION_GAP * 3,
            label="Reentrada semanal",
            content=CAMPAIGNS["pv.weekly_reentry"]["text"],
            median=3,
        ),
        dict(
            step_key="weekly.link2",
            block_key="weekly",
            branch_key=None,
            position=POSITION_GAP * 4,
            label="Link semanal 2",
            content="{preview_link}",
            median=3,
        ),
        dict(
            step_key="live.optin",
            block_key="live_optin",
            branch_key=None,
            position=POSITION_GAP,
            label="Opt-in da live",
            content=CAMPAIGNS["pv.live_optin"]["text"],
            median=20 * 60,
        ),
    ]
    for index, text in enumerate(LIVE_INVITE_VARIANTS):
        rows.append(
            dict(
                step_key=f"live.invite.v{index + 1}",
                block_key="live_invite",
                branch_key=f"live_invite:v{index + 1}",
                position=POSITION_GAP,
                label=f"Convite live {index + 1}",
                content=text,
                median=0,
                variant_root=True,
            )
        )
    for index, text in enumerate(LIVE_REMARKETING_VARIANTS):
        rows.append(
            dict(
                step_key=f"live.remarketing.v{index + 1}",
                block_key="live_remarketing",
                branch_key=f"live_remarketing:v{index + 1}",
                position=POSITION_GAP,
                label=f"Remarketing live {index + 1}",
                content=text,
                median=10 * 60,
                variant_root=True,
            )
        )
    rows += [
        dict(
            step_key="live.link",
            block_key="live_link",
            branch_key=None,
            position=POSITION_GAP,
            label="Link da live",
            content="{live_link}",
            median=0,
        ),
        dict(
            step_key="two.prompt",
            block_key="two_screens_prompt",
            branch_key=None,
            position=POSITION_GAP,
            label="Duas telas",
            content=CAMPAIGNS["pv.two_screens.prompt"]["text"],
            median=20,
        ),
        dict(
            step_key="two.prompt.preference",
            block_key="two_screens_prompt",
            branch_key=None,
            position=POSITION_GAP * 2,
            label="Preferência após abertura",
            content=DIALOGS["pv.two_screens.preference"],
            median=3,
        ),
        dict(
            step_key="two.preference",
            block_key="two_screens_preference",
            branch_key=None,
            position=POSITION_GAP,
            label="Preferência duas telas",
            content=DIALOGS["pv.two_screens.preference"],
            median=0,
        ),
        dict(
            step_key="two.question",
            block_key="two_screens_question",
            branch_key=None,
            position=POSITION_GAP,
            label="Pergunta duas telas",
            content=CAMPAIGNS["pv.two_screens.question"]["text"],
            median=0,
        ),
        dict(
            step_key="two.limit",
            block_key="two_screens_limit",
            branch_key=None,
            position=POSITION_GAP,
            label="Limite duas telas",
            content=CAMPAIGNS["pv.two_screens.limit"]["text"],
            median=6,
        ),
        dict(
            step_key="two.followup",
            block_key="two_screens_followup",
            branch_key=None,
            position=POSITION_GAP,
            label="Follow-up duas telas",
            content=CAMPAIGNS["pv.two_screens.followup"]["text"],
            median=6,
        ),
        dict(
            step_key="two.retry.preference",
            block_key="two_screens_retry_preference",
            branch_key=None,
            position=POSITION_GAP,
            label="Retry preferência ativa",
            content=DIALOGS["pv.two_screens.preference"],
            median=0,
        ),
        dict(
            step_key="two.retry",
            block_key="two_screens_retry",
            branch_key=None,
            position=POSITION_GAP,
            label="Retry duas telas",
            content=CAMPAIGNS["pv.two_screens.retry"]["text"],
            median=0,
        ),
    ]
    for slot in ("peitos", "buceta", "cu"):
        rows.append(
            dict(
                step_key=f"photo.caption.{slot}",
                block_key=f"photo_caption_{slot}",
                branch_key=None,
                position=POSITION_GAP,
                label=f"Legenda {slot}",
                content=caption_for_slot(slot),
                median=3,
            )
        )
    return rows


def _branch_sort_key(branch_key: str | None) -> tuple[str, int, str]:
    value = str(branch_key or "")
    match = _TRAILING_NUMBER_RE.search(value)
    return (
        value[: match.start()] if match else value,
        int(match.group(1)) if match else -1,
        value,
    )


class PvMessageStepStore:
    def __init__(self, pool, settings):
        self.pool = pool
        self.settings = settings
        self._ready = False

    async def ensure_ready(self) -> None:
        if self._ready:
            return
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(DDL)
                applied = await conn.fetchval(
                    "SELECT 1 FROM pv_message_step_migrations WHERE version=$1",
                    SEED_VERSION,
                )
                if not applied:
                    for row in _baseline(self.settings):
                        await conn.execute(
                            """INSERT INTO pv_message_steps(
                               step_key,block_key,branch_key,position,label,content,
                               kind,median_delay_seconds,variant_root,built_in)
                               VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE)
                               ON CONFLICT(step_key) DO NOTHING""",
                            row["step_key"],
                            row["block_key"],
                            row.get("branch_key"),
                            row["position"],
                            row["label"],
                            row["content"],
                            infer_kind(row["content"]),
                            int(row["median"]),
                            bool(row.get("variant_root", False)),
                        )
                    await conn.execute(
                        """INSERT INTO pv_message_step_migrations(version)
                           VALUES($1) ON CONFLICT DO NOTHING""",
                        SEED_VERSION,
                    )
        self._ready = True

    async def list_all(self):
        await self.ensure_ready()
        rows = await self.pool.fetch(
            "SELECT * FROM pv_message_steps ORDER BY block_key,position,id"
        )
        order = {name: idx for idx, name in enumerate(BLOCK_ORDER)}
        return sorted(
            rows,
            key=lambda row: (
                order.get(str(row["block_key"]), 999),
                _branch_sort_key(row["branch_key"]),
                Decimal(row["position"]),
                int(row["id"]),
            ),
        )

    async def get(self, step_id: int):
        await self.ensure_ready()
        return await self.pool.fetchrow(
            "SELECT * FROM pv_message_steps WHERE id=$1", int(step_id)
        )

    async def block_rows(
        self, block_key: str, branch_key: str | None = None
    ):
        await self.ensure_ready()
        if branch_key is None:
            return await self.pool.fetch(
                """SELECT * FROM pv_message_steps
                   WHERE block_key=$1 AND branch_key IS NULL
                   ORDER BY position,id""",
                block_key,
            )
        return await self.pool.fetch(
            """SELECT * FROM pv_message_steps
               WHERE block_key=$1 AND branch_key=$2 ORDER BY position,id""",
            block_key,
            branch_key,
        )

    async def variant_roots(self, block_key: str):
        await self.ensure_ready()
        return await self.pool.fetch(
            """SELECT * FROM pv_message_steps
               WHERE block_key=$1 AND variant_root=TRUE ORDER BY id""",
            block_key,
        )

    async def choose_branch(self, block_key: str, stable_key: str) -> str | None:
        roots = await self.variant_roots(block_key)
        if not roots:
            return None
        digest = hashlib.sha256(stable_key.encode("utf-8")).digest()
        root = roots[int.from_bytes(digest[:8], "big") % len(roots)]
        return str(root["branch_key"])

    async def first_step(
        self, block_key: str, branch_key: str | None = None
    ):
        rows = await self.block_rows(block_key, branch_key)
        return rows[0] if rows else None

    async def next_step(
        self, block_key: str, branch_key: str | None, after_position
    ):
        await self.ensure_ready()
        if branch_key is None:
            return await self.pool.fetchrow(
                """SELECT * FROM pv_message_steps
                   WHERE block_key=$1 AND branch_key IS NULL AND position>$2
                   ORDER BY position,id LIMIT 1""",
                block_key,
                Decimal(str(after_position)),
            )
        return await self.pool.fetchrow(
            """SELECT * FROM pv_message_steps
               WHERE block_key=$1 AND branch_key=$2 AND position>$3
               ORDER BY position,id LIMIT 1""",
            block_key,
            branch_key,
            Decimal(str(after_position)),
        )

    async def set_content(self, step_id: int, content: str) -> str:
        """Replace a balloon with text/link while preserving its identity and order."""
        await self.ensure_ready()
        value = (content or "").strip()
        if not value:
            deleted = await self.delete(step_id)
            return "deleted" if deleted else "missing"
        updated = await self.pool.fetchval(
            """UPDATE pv_message_steps
               SET content=$2,kind=$3,media_source_peer=NULL,
                   media_source_message_id=NULL,media_kind=NULL,updated_at=NOW()
               WHERE id=$1 RETURNING id""",
            int(step_id),
            value,
            infer_kind(value),
        )
        return "updated" if updated else "missing"

    async def set_payload(
        self,
        step_id: int,
        content: str,
        *,
        media_source_peer: int,
        media_source_message_id: int,
        media_kind: str,
    ) -> str:
        """Replace content in place with media plus an optional companion text."""
        await self.ensure_ready()
        value = (content or "").strip()
        updated = await self.pool.fetchval(
            """UPDATE pv_message_steps
               SET content=$2,kind=$3,media_source_peer=$4,
                   media_source_message_id=$5,media_kind=$6,updated_at=NOW()
               WHERE id=$1 RETURNING id""",
            int(step_id),
            value,
            infer_kind(value),
            int(media_source_peer),
            int(media_source_message_id),
            str(media_kind),
        )
        return "updated" if updated else "missing"

    async def set_delay(self, step_id: int, median_seconds: int) -> bool:
        await self.ensure_ready()
        median = int(median_seconds)
        if median < 0 or median > MAX_MEDIAN_SECONDS:
            raise ValueError("tempo fora do limite")
        updated = await self.pool.fetchval(
            """UPDATE pv_message_steps
               SET median_delay_seconds=$2,updated_at=NOW()
               WHERE id=$1 RETURNING id""",
            int(step_id),
            median,
        )
        return updated is not None

    async def delete(self, step_id: int) -> bool:
        """Delete only this speech; a surviving variant branch keeps one root."""
        await self.ensure_ready()
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    """DELETE FROM pv_message_steps WHERE id=$1
                       RETURNING id,variant_root,block_key,branch_key""",
                    int(step_id),
                )
                if not row:
                    return False
                if bool(row["variant_root"]) and row["branch_key"]:
                    replacement = await conn.fetchval(
                        """SELECT id FROM pv_message_steps
                           WHERE block_key=$1 AND branch_key=$2
                           ORDER BY position,id LIMIT 1""",
                        row["block_key"],
                        row["branch_key"],
                    )
                    if replacement is not None:
                        await conn.execute(
                            """UPDATE pv_message_steps SET variant_root=TRUE,
                               updated_at=NOW() WHERE id=$1""",
                            int(replacement),
                        )
                return True

    async def add_after(
        self,
        step_id: int,
        content: str,
        median_seconds: int,
        *,
        label: str = "Nova fala",
        media_source_peer: int | None = None,
        media_source_message_id: int | None = None,
        media_kind: str | None = None,
    ):
        await self.ensure_ready()
        value = (content or "").strip()
        has_media = media_source_peer is not None and media_source_message_id is not None
        if not value and not has_media:
            raise ValueError("fala vazia")
        median = int(median_seconds)
        if median < 0 or median > MAX_MEDIAN_SECONDS:
            raise ValueError("tempo fora do limite")
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                parent = await conn.fetchrow(
                    "SELECT * FROM pv_message_steps WHERE id=$1 FOR UPDATE",
                    int(step_id),
                )
                if not parent:
                    return None
                if parent["branch_key"] is None:
                    nxt = await conn.fetchval(
                        """SELECT position FROM pv_message_steps
                           WHERE block_key=$1 AND branch_key IS NULL
                             AND position>$2 ORDER BY position LIMIT 1""",
                        parent["block_key"],
                        parent["position"],
                    )
                else:
                    nxt = await conn.fetchval(
                        """SELECT position FROM pv_message_steps
                           WHERE block_key=$1 AND branch_key=$2 AND position>$3
                           ORDER BY position LIMIT 1""",
                        parent["block_key"],
                        parent["branch_key"],
                        parent["position"],
                    )
                parent_pos = Decimal(parent["position"])
                next_pos = (
                    Decimal(nxt)
                    if nxt is not None
                    else parent_pos + POSITION_GAP
                )
                position = (parent_pos + next_pos) / 2
                step_key = f"custom.{uuid.uuid4().hex}"
                return await conn.fetchrow(
                    """INSERT INTO pv_message_steps(
                       step_key,block_key,branch_key,position,label,content,kind,
                       median_delay_seconds,variant_root,built_in,
                       media_source_peer,media_source_message_id,media_kind)
                       VALUES($1,$2,$3,$4,$5,$6,$7,$8,FALSE,FALSE,$9,$10,$11)
                       RETURNING *""",
                    step_key,
                    parent["block_key"],
                    parent["branch_key"],
                    position,
                    label,
                    value,
                    infer_kind(value),
                    median,
                    int(media_source_peer) if media_source_peer is not None else None,
                    int(media_source_message_id) if media_source_message_id is not None else None,
                    str(media_kind) if media_kind is not None else None,
                )

    def render(
        self,
        content: str,
        *,
        preview_link: str = "",
        live_link: str = "",
    ) -> str:
        return (
            (content or "")
            .replace("{preview_link}", preview_link or "")
            .replace("{live_link}", live_link or "")
        )
