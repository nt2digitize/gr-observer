# Homologação — contatos, ADD e proteção de engajamento

Este pacote deve entrar em produção com todas as funções novas desligadas. A ativação é progressiva e reversível por variável de ambiente.

## Etapa 0 — deploy inerte
Manter `PV_AUTO_SAVE_CONTACTS=false`, `GROUP_ADD_CONTACT_FLOW=false` e `GROUP_ENGAGEMENT_PROTECTION=false`. Validar serviço e sessão única.

## Etapa 1 — autosalvamento PV
Ativar somente `PV_AUTO_SAVE_CONTACTS=true`. Confirmar contato salvo, ausência de duplicação, funcionamento sem telefone visível, nenhuma mensagem extra e nenhum FloodWait anormal.

## Etapa 2 — ADD em grupo autorizado
Ativar `GROUP_ADD_CONTACT_FLOW=true`. Testar reply `add, tô de ban` e `@username me adiciona`. O contato deve ser salvo antes da resposta `já add, chama lá`; `add` sem reply/menção não dispara e falha de salvamento não confirma.

## Etapa 3 — proteção de engajamento
Usar temporariamente `GROUP_ENGAGEMENT_PROTECTION=true` e `GROUP_ENGAGEMENT_PROTECTION_SECONDS=120`.

1. responder diretamente à publicação corrente do loop;
2. confirmar que o próximo repost preserva a antiga;
3. responder novamente antes de 120 s e confirmar renovação;
4. confirmar que limpeza antiga não apaga a renovada;
5. após expirar, remover somente se não for mais corrente;
6. confirmar que atendimento reativo comum não vira alvo de limpeza;
7. enviar menção solta `@username você é do RJ?` sem reply e confirmar que **não protege** o post corrente.

Depois da homologação, restaurar `GROUP_ENGAGEMENT_PROTECTION_SECONDS=86400`.

## Kill switch
Em comportamento inesperado, desligar as três flags. Salvamento/ADD pendentes viram no-op após restart; limpezas já agendadas continuam.

## GO / NO-GO
GO somente com CI verde, sessão única saudável, zero confirmação falsa, nenhuma duplicidade, proteção renovável, menção solta sem proteção, nenhuma deleção fora do loop, FloodWait esperado e rollback por flags validado.
