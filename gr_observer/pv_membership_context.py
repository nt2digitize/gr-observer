"""Read-only membership context for the linear PV conversation.

This module never advances the conversation and never creates Telegram work.
It only translates the passive membership read model into a small context that
later copy-selection layers may use.
"""

from __future__ import annotations

from dataclasses import dataclass


BEFORE_JOIN = "before_join"
JOINED = "joined"
LEFT = "left"
REJOINED = "rejoined"

_ALLOWED_REPERTOIRES = {
    BEFORE_JOIN: ("pre_join",),
    JOINED: ("joined", "post_join"),
    LEFT: ("left", "post_exit"),
    REJOINED: ("rejoined", "post_join"),
}


@dataclass(frozen=True)
class MembershipConversationContext:
    state: str
    allowed_repertoires: tuple[str, ...]
    join_count: int = 0
    leave_count: int = 0


def _context(state: str, join_count: int = 0, leave_count: int = 0) -> MembershipConversationContext:
    return MembershipConversationContext(
        state=state,
        allowed_repertoires=_ALLOWED_REPERTOIRES[state],
        join_count=max(0, int(join_count)),
        leave_count=max(0, int(leave_count)),
    )


async def membership_conversation_context(pool, user_id: int) -> MembershipConversationContext:
    """Return current preview-group context without mutating any business state."""
    row = await pool.fetchrow(
        """SELECT status,join_count,leave_count,last_change_at
           FROM pv_group_membership_state
           WHERE user_id=$1 AND preview_group IS TRUE
           ORDER BY last_change_at DESC,updated_at DESC
           LIMIT 1""",
        int(user_id),
    )
    if row is None:
        return _context(BEFORE_JOIN)

    status = str(row["status"])
    join_count = int(row["join_count"] or 0)
    leave_count = int(row["leave_count"] or 0)

    if status == "left":
        return _context(LEFT, join_count, leave_count)
    if status == "joined" and (join_count > 1 or leave_count > 0):
        return _context(REJOINED, join_count, leave_count)
    if status == "joined":
        return _context(JOINED, join_count, leave_count)
    return _context(BEFORE_JOIN, join_count, leave_count)
