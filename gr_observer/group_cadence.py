"""Pure planning helpers for adaptive group repost cadence."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepostPlan:
    """One persisted repost-cycle decision.

    The plan is frozen for the current cycle so restarts do not silently change
    its target. A later human message may replace it only after validity expiry.
    """

    target_messages: int
    min_interval_seconds: int
    activity_score: float
    validity_seconds: int


def compute_repost_plan(
    recent_rate_per_hour: float,
    same_hour_rate_per_hour: float,
    daily_rate_per_hour: float,
) -> RepostPlan:
    """Build one conservative cadence plan from three activity signals.

    Weights follow the agreed model: 60% recent activity, 30% historical activity
    for the same weekday/hour, and 10% the rolling daily baseline. The output is
    deliberately bounded: very hot groups require more human messages but still
    have a minimum time gate; slow groups require fewer messages and a longer
    time gate. Time alone never triggers a repost -- the caller also requires a
    fresh human message.
    """

    recent = max(0.0, float(recent_rate_per_hour or 0.0))
    same_hour = max(0.0, float(same_hour_rate_per_hour or 0.0))
    daily = max(0.0, float(daily_rate_per_hour or 0.0))
    score = (0.60 * recent) + (0.30 * same_hour) + (0.10 * daily)

    # Scale the message gate with the expected human volume while keeping a
    # small base requirement. This is deliberately more conservative than a
    # raw percentage in medium/slow groups.
    target = 2 + round(score * 0.20)
    target = max(2, min(40, target))

    # Expected time to observe that target at the blended rate, with a 25%
    # safety margin. The floor keeps bursts from causing immediate reposts; the
    # ceiling avoids stale multi-day plans in quiet groups.
    effective_rate = max(score, 0.5)
    min_interval = round((target / effective_rate) * 3600 * 1.25)
    min_interval = max(15 * 60, min(4 * 3600, min_interval))

    # A cycle decision must survive short restarts, but should not remain valid
    # indefinitely after the group's time-of-day regime has changed.
    validity = max(2 * 3600, min(4 * 3600, min_interval))

    return RepostPlan(
        target_messages=target,
        min_interval_seconds=min_interval,
        activity_score=round(score, 3),
        validity_seconds=validity,
    )
