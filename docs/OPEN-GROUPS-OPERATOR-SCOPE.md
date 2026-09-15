# Escopo operacional — grupos de chat abertos

A interface diária do GR Observer prioriza somente grupos de chat em que a conta USER:

- ainda participa (`membership_status='joined'`);
- possui permissão atual de escrita (`can_text=TRUE`);
- e, para a lista de ativos, já possui `group_repost_state`.

Classificação visual:

- **Ativos**: dentro + escrita liberada + postagem-modelo registrada.
- **Revisar**: dentro + postagem-modelo registrada + escrita bloqueada ou incerta.
- **Livres, falta publicar**: dentro + escrita liberada + sem postagem-modelo.
- **Para conhecer**: candidato do tipo grupo, ainda não ingressado e sem aprovação pendente.

Canais, bots e listas técnicas de links continuam disponíveis internamente ao Radar para compatibilidade e coleta, mas não aparecem na operação diária.

A checagem é passiva: o Radar usa os diálogos e permissões reais do Telegram já consultados no ciclo existente. Não são enviadas mensagens de teste, não há novo worker, nova sessão ou novo Writer.
