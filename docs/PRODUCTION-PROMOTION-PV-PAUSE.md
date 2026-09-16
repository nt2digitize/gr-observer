# Promoção de produção — pausa individual de chat PV

- Commit funcional validado: `d5ccde9a5b8438dc0f11d4a2eb8b6d19cdabcdc5`
- CI: suíte completa + compilação Python verdes no PR #50.
- Escopo: somente UX da pausa individual do chat automático PV.
- Busca: @username conhecido ou Telegram user_id.
- Identificação preventiva: mensagem encaminhada com origem visível.
- Confirmação obrigatória antes de pausar.
- A confirmação mostra nome/@username conhecido e um botão clicável que abre o contato via `tg://user?id=...`.
- O Telegram user_id fica oculto na interface normal e continua sendo usado internamente como identidade estável.
- Persistência por Telegram user_id; apagar a mensagem original depois não reativa o chat.
- Não bloqueia no Telegram, não remove contato da agenda e não afeta outros chats.
- Nenhuma ativação de P00 incluída nesta promoção.
- Nenhuma alteração de USER_SESSION_STRING, API ID/hash/token ou PostgreSQL destrutiva.
