"""Priority/fairness policy for the single Outbox Writer.

Lower numeric priority wins. The policy changes only which ready action gets the
next turn; it never creates another Writer and never bypasses Telegram pacing or
TrafficGovernor cooldowns.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# P00 is reserved for a human just captured from the active group bait. It is
# intentionally above live PV work, but only among actions that are already
# eligible. available_at, pacing, TrafficGovernor and FloodWait remain sovereign.
P00_GROUP_CAPTURE = -1
P0_LIVE_HUMAN = 0
P1_HUMAN_REACTIVE = 1
P2_NORMAL = 2
P3_DEFERRED = 3
P4_BACKGROUND = 4

AGING_STEP_SECONDS = 180
LANE_REPEAT_PENALTY = 1
FRESH_HUMAN_TTL_SECONDS = 300

# Provenance is already encoded in deterministic Outbox keys created only from
# inbound-human branches of Storage.accept_pv_message. This avoids a second
# write/race merely to decorate an already-persisted action.
HUMAN_ORIGIN_PREFIXES: dict[tuple[str, str], str] = {
    ("pv_reply", "send_reminder_link"): "pv_reply:conditional-link:",
    ("pv_reply", "send_live_link"): "pv_reply:live-link:",
}

ACTION_PRIORITY: dict[tuple[str, str], int] = {
    # A human has just engaged the active bait in an authorized group.
    ("group_reply", "group_capture_contact_reply"): P00_GROUP_CAPTURE,
    # A human is actively waiting in PV by definition of the action.
    ("pv_reply", "send_greeting"): P0_LIVE_HUMAN,
    ("pv_reply", "send_link"): P0_LIVE_HUMAN,
    ("pv_reply", "send_two_screens_question"): P0_LIVE_HUMAN,
    ("pv_reply", "send_two_screens_retry"): P0_LIVE_HUMAN,
    # Conditional P0 actions fall back here after the human-recent window.
    ("pv_reply", "send_reminder_link"): P2_NORMAL,
    ("pv_reply", "send_live_link"): P2_NORMAL,
    # A human is actively waiting in a group, or a PV flow is continuing live.
    ("group_reply", "send_group_reply"): P1_HUMAN_REACTIVE,
    ("group_reply", "group_add_contact_reply"): P1_HUMAN_REACTIVE,
    ("pv_reply", "send_two_screens_photo"): P1_HUMAN_REACTIVE,
    # Normal conversational automation, not caused by a fresh inbound turn.
    ("pv_reply", "send_two_screens_prompt"): P2_NORMAL,
    ("pv_reply", "send_two_screens_limit"): P2_NORMAL,
    ("pv_reply", "send_two_screens_followup"): P2_NORMAL,
    ("pv_reply", "auto_queue_two_screens_photo"): P2_NORMAL,
    ("pv_reply", "send_live_optin"): P2_NORMAL,
    ("core", "notify_admin"): P2_NORMAL,
    # Deferred marketing / follow-up work.
    ("pv_reply", "send_followup"): P3_DEFERRED,
    ("pv_reply", "send_weekly_question"): P3_DEFERRED,
    ("pv_reply", "send_live_invite"): P3_DEFERRED,
    ("pv_reply", "send_live_remarketing"): P3_DEFERRED,
    # Housekeeping and visibility maintenance.
    ("group_reply", "repost_group_text"): P4_BACKGROUND,
    ("group_reply", "cleanup_protected_group_message"): P4_BACKGROUND,
    ("group_reply", "group_capture_cleanup"): P4_BACKGROUND,
    ("pv_reply", "close_live_recipient"): P4_BACKGROUND,
}

DEFAULT_PRIORITY = P2_NORMAL


def base_priority(
    module_id: str,
    action_type: str,
    *,
    action_key: str = "",
    origin_age_seconds: float = 0.0,
) -> int:
    key = (str(module_id), str(action_type))
    prefix = HUMAN_ORIGIN_PREFIXES.get(key)
    if (
        prefix
        and str(action_key).startswith(prefix)
        and 0.0 <= float(origin_age_seconds) <= FRESH_HUMAN_TTL_SECONDS
    ):
        return P0_LIVE_HUMAN
    return ACTION_PRIORITY.get(key, DEFAULT_PRIORITY)


def lane_key(module_id: str, payload: dict[str, Any] | None) -> str:
    payload = payload or {}
    peer = payload.get("peer")
    if peer is not None and module_id == "pv_reply":
        return f"pv:{peer}"
    if peer is not None and module_id == "group_reply":
        return f"group:{peer}"
    return str(module_id)


def effective_priority(
    module_id: str,
    action_type: str,
    *,
    wait_seconds: float,
    lane: str,
    last_lane: str | None,
    action_key: str = "",
    origin_age_seconds: float = 0.0,
) -> int:
    """Age normal ready work upward without manufacturing P00 captures.

    P00 is a semantic class, not an aging destination. A real P00 also ignores
    the one-turn lane penalty: the capture must stay above PV P0 once it is
    ready, while still sharing the same serial Writer and global traffic gates.
    """
    base = base_priority(
        module_id,
        action_type,
        action_key=action_key,
        origin_age_seconds=origin_age_seconds,
    )
    if base == P00_GROUP_CAPTURE:
        return P00_GROUP_CAPTURE
    age_steps = max(0, int(wait_seconds // AGING_STEP_SECONDS))
    aged = max(P0_LIVE_HUMAN, base - min(base, age_steps))
    repeat_penalty = LANE_REPEAT_PENALTY if last_lane and lane == last_lane else 0
    return aged + repeat_penalty


def priority_case_sql(alias: str = "actions") -> str:
    grouped: dict[int, list[tuple[str, str]]] = {}
    for key, priority in ACTION_PRIORITY.items():
        grouped.setdefault(priority, []).append(key)
    branches: list[str] = ["CASE"]
    for (module, action), prefix in sorted(HUMAN_ORIGIN_PREFIXES.items()):
        branches.append(
            f"WHEN {alias}.module_id='{module}' "
            f"AND {alias}.action_type='{action}' "
            f"AND {alias}.action_key LIKE '{prefix}%' "
            f"AND {alias}.created_at >= NOW()-INTERVAL '{FRESH_HUMAN_TTL_SECONDS} seconds' "
            f"THEN {P0_LIVE_HUMAN}"
        )
    for priority in sorted(grouped):
        conditions = [
            f"({alias}.module_id='{module}' AND {alias}.action_type='{action}')"
            for module, action in sorted(grouped[priority])
        ]
        branches.append(f"WHEN {' OR '.join(conditions)} THEN {priority}")
    branches.append(f"ELSE {DEFAULT_PRIORITY} END")
    return " ".join(branches)


def lane_expr_sql(alias: str = "actions") -> str:
    return (
        "CASE "
        f"WHEN {alias}.module_id='pv_reply' AND {alias}.payload ? 'peer' "
        f"THEN 'pv:' || ({alias}.payload->>'peer') "
        f"WHEN {alias}.module_id='group_reply' AND {alias}.payload ? 'peer' "
        f"THEN 'group:' || ({alias}.payload->>'peer') "
        f"ELSE {alias}.module_id END"
    )


@dataclass(frozen=True)
class SimAction:
    key: str
    module_id: str
    action_type: str
    available_at: float
    peer: int | str | None = None
    created_at: float | None = None

    @property
    def lane(self) -> str:
        payload = {} if self.peer is None else {"peer": self.peer}
        return lane_key(self.module_id, payload)


def choose_ready_action(
    actions: list[SimAction], *, now: float, last_lane: str | None
) -> SimAction | None:
    ready = [action for action in actions if action.available_at <= now]
    if not ready:
        return None

    def sort_key(action: SimAction):
        created_at = action.available_at if action.created_at is None else action.created_at
        return (
            effective_priority(
                action.module_id,
                action.action_type,
                wait_seconds=max(0.0, now - action.available_at),
                lane=action.lane,
                last_lane=last_lane,
                action_key=action.key,
                origin_age_seconds=max(0.0, now - created_at),
            ),
            action.available_at,
            action.key,
        )

    return min(ready, key=sort_key)
