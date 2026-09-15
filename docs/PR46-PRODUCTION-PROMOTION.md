# PR46 production promotion

Promotion marker for the MiniLearn passive PV shadow listener.

- Source merge: `1f2b54f3731b1a66fe34098cdb2efce7828535e8`
- Shadow listener code deploys with `PV_MINILEARN_SHADOW_ENABLED=false` by default.
- No additional Telegram sends, Writer, Outbox, session, worker or Railway service.
- Purpose: force Railway to build the current `release/pr16-production` source without applying unrelated staged Railway configuration changes.
