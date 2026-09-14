"""Deterministic bench for PR #35. No Telegram or database access is used."""

from __future__ import annotations

from dataclasses import dataclass

from gr_observer.queue_policy import SimAction, choose_ready_action

WRITER_SLOT_SECONDS = 20.0
DEFAULT_PV_DELAY_SECONDS = 60.0
DEFAULT_PV_CALL_AT = 40.0


@dataclass(frozen=True)
class BenchResult:
    contacts: int
    mode: str
    pv_claim_at: float
    pv_wait_from_inbound: float
    pv_queue_overhead: float
    claims_before_pv: int


def build_group_to_pv_scenario(
    contacts: int,
    *,
    pv_call_at: float = DEFAULT_PV_CALL_AT,
    pv_delay_seconds: float = DEFAULT_PV_DELAY_SECONDS,
    include_background: bool = False,
) -> tuple[list[SimAction], str, float]:
    actions: list[SimAction] = []
    for index in range(max(0, int(contacts))):
        # Mirrors the real stable ADD window: 25..35 seconds.
        available = 25.0 + float(index % 11)
        group_peer = -1001 if index % 2 == 0 else -1002
        actions.append(
            SimAction(
                key=f"group-add:{index:03d}",
                module_id="group_reply",
                action_type="group_add_contact_reply",
                available_at=available,
                peer=group_peer,
            )
        )
    if include_background:
        for index in range(3):
            actions.append(
                SimAction(
                    key=f"old-followup:{index}",
                    module_id="pv_reply",
                    action_type="send_followup",
                    available_at=0.0,
                    peer=7000 + index,
                )
            )
        for index in range(2):
            actions.append(
                SimAction(
                    key=f"old-repost:{index}",
                    module_id="group_reply",
                    action_type="repost_group_text",
                    available_at=0.0,
                    peer=-2000 - index,
                )
            )
    pv_key = "pv:greeting:new-contact"
    pv_available = pv_call_at + pv_delay_seconds
    actions.append(
        SimAction(
            key=pv_key,
            module_id="pv_reply",
            action_type="send_greeting",
            available_at=pv_available,
            peer=999001,
        )
    )
    return actions, pv_key, pv_available


def run_bench(
    contacts: int,
    *,
    prioritized: bool,
    include_background: bool = False,
    pv_call_at: float = DEFAULT_PV_CALL_AT,
    pv_delay_seconds: float = DEFAULT_PV_DELAY_SECONDS,
    writer_slot_seconds: float = WRITER_SLOT_SECONDS,
) -> BenchResult:
    pending, pv_key, pv_available = build_group_to_pv_scenario(
        contacts,
        pv_call_at=pv_call_at,
        pv_delay_seconds=pv_delay_seconds,
        include_background=include_background,
    )
    now = min(action.available_at for action in pending)
    last_lane: str | None = None
    claims = 0
    while pending:
        ready = [action for action in pending if action.available_at <= now]
        if not ready:
            now = min(action.available_at for action in pending)
            ready = [action for action in pending if action.available_at <= now]
        if prioritized:
            chosen = choose_ready_action(pending, now=now, last_lane=last_lane)
            assert chosen is not None
        else:
            chosen = min(ready, key=lambda action: (action.available_at, action.key))
        if chosen.key == pv_key:
            return BenchResult(
                contacts=contacts,
                mode="priority" if prioritized else "fifo",
                pv_claim_at=now,
                pv_wait_from_inbound=now - pv_call_at,
                pv_queue_overhead=max(0.0, now - pv_available),
                claims_before_pv=claims,
            )
        pending.remove(chosen)
        last_lane = chosen.lane
        claims += 1
        now += writer_slot_seconds
    raise AssertionError("PV greeting was never claimed")


def matrix(include_background: bool = False) -> list[tuple[BenchResult, BenchResult]]:
    return [
        (
            run_bench(size, prioritized=False, include_background=include_background),
            run_bench(size, prioritized=True, include_background=include_background),
        )
        for size in (1, 5, 10, 20)
    ]


def main() -> None:
    for background in (False, True):
        print("mixed backlog" if background else "group burst only")
        print("contacts fifo_wait priority_wait priority_queue_overhead claims_before_pv")
        for fifo, priority in matrix(include_background=background):
            print(
                f"{fifo.contacts:>8} {fifo.pv_wait_from_inbound:>9.0f}s "
                f"{priority.pv_wait_from_inbound:>12.0f}s "
                f"{priority.pv_queue_overhead:>23.0f}s "
                f"{priority.claims_before_pv:>16}"
            )


if __name__ == "__main__":
    main()
