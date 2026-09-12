# ADR-003 — Review estrito e costelinhas lógicas do Atendimento PV

- **Status:** proposto/implementado na branch de correção
- **Data:** 2026-09-12
- **Escopo:** `nt2digitize/gr-observer`

## Contexto

`pv_reply` é uma costela arquitetural única do monólito. Dentro dela existem várias conversas privadas independentes. Tratar uma pessoa aguardando resposta, falhando ou entrando em revisão como se pudesse reter a fila global cria acoplamento indevido e dificulta diagnosticar o andamento real do atendimento.

Também havia uma semântica excessivamente conservadora no Writer: praticamente qualquer exceção ocorrida depois de abrir o diário de efeito podia virar `review`, inclusive rejeições RPC conclusivas do Telegram.

## Decisão

Cada `user_id` do Atendimento PV é uma **costelinha lógica** (lane) dentro da costela `pv_reply`. Isso não cria módulos, clientes Telegram, workers ou Writers adicionais. O estado continua persistido por contato e a Outbox global continua selecionando qualquer ação pronta por `available_at` com `SKIP LOCKED`.

Uma conversa em espera, falha ou revisão afeta somente aquela lane. Não existe trava global por resposta humana.

O estado `review` fica reservado para resultado externo realmente incerto: timeout, desconexão, cancelamento ou erro de servidor em uma janela onde não é possível provar se o Telegram aplicou a mutação.

Tratamento do Writer:

- `FloodWait`: aguarda o tempo informado pelo Telegram + buffer e tenta a mesma ação novamente;
- rejeição RPC conclusiva 4xx (`BadRequest`, `Unauthorized`, `Forbidden`, `NotFound`): falha conhecida da ação, **não** entra em review;
- validação local determinística antes do envio: falha conhecida, **não** entra em review;
- timeout/desconexão/resultado incerto: `review`;
- uma lane em `failed` ou `review` não impede o Writer de buscar a próxima ação pronta de outra lane.

## Idempotência

Uma rejeição conclusiva é registrada como resultado terminal resolvido no diário do efeito para que uma repetição da mesma chave não reenvie a mutação. O resultado carrega explicitamente `_effect_outcome=rejected`; o Writer converte isso novamente em falha conhecida caso a mesma chave seja reencontrada.

Não há repetição cega de efeitos ambíguos.

## Consequências

- `review` volta a significar somente "não sei se o Telegram executou";
- erros concretos deixam de acumular quarentenas falsas;
- logs passam a identificar a lane `pv:<user_id>`;
- o pacing global e a proteção de FloodWait permanecem inalterados;
- nenhuma conversa humana aguardando resposta mantém uma ação `processing` aberta.
