# ADR-008 — Contrato transversal de idempotência e rollback

- **Status:** proposto nesta branch
- **Data:** 2026-09-15
- **Escopo:** `nt2digitize/gr-observer`
- **Depende de:** ADR-001, ADR-003, ADR-004, PR #35 e arquitetura espinha de peixe

## Contexto

Novas funções de PV e grupos aumentam a quantidade de intenções concorrentes na mesma conta Telegram. Antes de ampliar a captura reativa de grupos, foi revisada a coluna dorsal com a mesma lente usada nas costelas: duplicação, retry após restart, corrida entre lanes, FloodWait, estado parcial e rollback isolado.

## Resultado da auditoria

Não foi encontrado motivo para criar uma segunda fila, segundo Writer ou mecanismo paralelo. A coluna dorsal já possui as proteções centrais necessárias:

- `inbox_events` deduplica o update de entrada por `(source,event_key)`;
- `outbox_actions.action_key` é único;
- `telegram_effects.effect_key` é chave primária;
- efeitos externos são registrados antes da mutação Telegram;
- ação interrompida em `processing` volta como `review`, nunca como replay cego;
- o claim de produção usa `PriorityStorage`, transação e `FOR UPDATE ... SKIP LOCKED`;
- `available_at` continua gate absoluto;
- uma única sessão USER e um único Writer permanecem protegidos pela composição e pelo lock da sessão;
- FloodWait e pacing continuam soberanos à prioridade;
- supressão individual de PV neutraliza ações pendentes sem apagar o histórico.

## Decisão

Transformar essas propriedades em contrato explícito de CI transversal. Qualquer mudança futura em PV, grupos, Radar ou BOTSON deve preservar:

1. intenção idempotente antes de qualquer mutação Telegram;
2. chave determinística para ação e efeito quando houver possibilidade de retry;
3. nenhum replay automático de efeito externo ambíguo;
4. nenhuma costela criando cliente USER, Outbox ou Writer próprios;
5. `available_at`, Governor, pacing e FloodWait acima da prioridade de negócio;
6. rollback de feature por branch/PR sem necessidade de desmontar outras costelas.

## Limites conhecidos

Nenhuma arquitetura consegue desfazer uma RPC que já tenha sido aceita pelo Telegram no exato instante em que um operador pausa uma pessoa ou um módulo. Por isso, o contrato garante neutralização do trabalho **pendente** e evita repetir efeitos ambíguos; uma chamada já em voo pode concluir uma única vez.

## Rollout

Esta ADR não altera comportamento Telegram. Ela adiciona documentação e testes estruturais. Pode ser promovida independentemente das features `pv-contact-pause-control` e `group-capture-p00` e pode ser revertida isoladamente sem migração de dados.
