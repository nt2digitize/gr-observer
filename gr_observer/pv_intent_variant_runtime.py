"""Runtime adapter for retry-stable intent variants in linear PV text balloons."""

from __future__ import annotations

import logging

from .pv_intent_variants import PvIntentVariantStore
from .pv_linear_flow import linear_flow_enabled


log = logging.getLogger("gr-observer.pv-intent-variants")


class PvIntentVariantRuntimeMixin:
    """Replace only configured linear text copy; never own conversation progress."""

    def __init__(self, storage, settings):
        self.intent_variant_store = PvIntentVariantStore(storage.pool)
        self._intent_variant_store_ready = False
        super().__init__(storage, settings)

    async def on_connect(self, client, me) -> None:
        await super().on_connect(client, me)
        try:
            await self.intent_variant_store.ensure_ready()
        except Exception as exc:
            # Variant copy is optional. Canonical copy remains the safe fallback.
            self._intent_variant_store_ready = False
            log.warning("PV intent variants unavailable erro=%s", type(exc).__name__)
        else:
            self._intent_variant_store_ready = True

    async def on_disconnect(self) -> None:
        self._intent_variant_store_ready = False
        await super().on_disconnect()

    async def _send_row(
        self,
        *,
        effects,
        peer: int,
        row,
        origin_key: str,
        variables: dict,
    ) -> dict:
        effective_row = row
        linear_send = linear_flow_enabled() and str(origin_key).startswith("pv_reply:linear:")
        repertoires = tuple((variables or {}).get("_membership_repertoires") or ())
        if linear_send and repertoires and self._intent_variant_store_ready:
            try:
                selected = await self.intent_variant_store.select_for_action(
                    action_key=str(origin_key),
                    user_id=int(peer),
                    step_id=int(row["id"]),
                    intent_keys=repertoires,
                )
            except Exception as exc:
                # Selection is best-effort; never block the canonical balloon.
                log.warning("PV intent variant fallback peer=%s erro=%s", int(peer), type(exc).__name__)
            else:
                if selected is not None:
                    effective_row = dict(row)
                    effective_row["content"] = selected.content
                    log.info(
                        "PV intent variant peer=%s intent=%s slot=%s step=%s",
                        int(peer),
                        selected.intent_key,
                        selected.slot,
                        int(row["id"]),
                    )

        return await super()._send_row(
            effects=effects,
            peer=peer,
            row=effective_row,
            origin_key=origin_key,
            variables=variables,
        )
