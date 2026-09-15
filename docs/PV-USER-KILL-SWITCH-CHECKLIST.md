# Gate de promoção — kill switch PV

1. CI completo verde.
2. `/parar_usuario <ID|@username>` resolve um único peer.
3. `pv_reply_contacts.stage` vira `stopped`.
4. live e duas-telas desse peer ficam parados.
5. Outbox pendente vira no-op auditável (`admin_suppressed`).
6. novas mensagens do peer não avançam a jornada.
7. uma sessão USER, uma Outbox e um Writer permanecem intactos.
8. nenhuma alteração de segredo, sessão ou credencial.
