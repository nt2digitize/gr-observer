# Promoção de produção — pausa individual de chat PV

- Commit funcional validado: `bb9e275feeb94916e2eae998c3694d17f9021f5e`
- CI: 312 testes + compilação Python verdes.
- Escopo: somente pausa individual do chat automático PV.
- Busca: @username conhecido ou Telegram user_id.
- Identificação preventiva: mensagem encaminhada com origem visível.
- Confirmação obrigatória antes de pausar.
- Persistência por Telegram user_id; apagar a mensagem original depois não reativa o chat.
- Não bloqueia no Telegram, não remove contato da agenda e não afeta outros chats.
- Nenhuma ativação de P00 incluída nesta promoção.
- Nenhuma alteração de USER_SESSION_STRING, API ID/hash/token ou PostgreSQL destrutiva.
