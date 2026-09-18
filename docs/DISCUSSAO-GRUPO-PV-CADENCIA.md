# Discussão — Cadência e prioridade Grupo → PV

- **Status:** em discussão
- **Data:** 2026-09-18
- **Escopo:** `nt2digitize/gr-observer`
- **Objetivo operacional:** aumentar a conversão de interações em grupos para conversas no PV, preservando a sessão única, Outbox única, Writer único, pacing global e resposta a `FLOOD_WAIT`/`SLOWMODE`.

## Contexto

O objetivo principal desta etapa é responder a gatilhos humanos em grupos com rapidez suficiente para aumentar a chance de o usuário migrar para o PV. Depois que o usuário já chamou no PV, a conversão principal da etapa Grupo → PV já aconteceu.

A discussão surgiu porque tempos conservadores e o intervalo global do Writer podem aumentar a latência entre a fala no grupo e a resposta automática. Ao mesmo tempo, a redução desses tempos não pode ignorar os mecanismos de segurança já existentes.

## Estado atual aplicado em produção

Em 2026-09-18 foram ajustadas somente as variáveis operacionais do Atendimento de Grupos no Railway:

- `GROUP_REPLY_MIN_DELAY_SECONDS=25`
- `GROUP_REPLY_MAX_DELAY_SECONDS=45`
- `GROUP_REPLY_MIN_COOLDOWN_SECONDS=60`
- `GROUP_REPLY_MAX_COOLDOWN_SECONDS=90`

Essas alterações não mudam arquitetura, sessão, Writer, Outbox, credenciais ou schema.

## Estado atual de prioridade

Permanece em vigor a política documentada no scheduler prioritário existente:

- P0: humano aguardando resposta no PV;
- P1: resposta humana reativa em grupo e continuação PV ativa;
- P3: follow-up/semanal/campanha diferida;
- P4: repost, limpeza e manutenção.

Portanto, **não foi aplicada inversão de prioridade** nesta discussão.

## Hipóteses ainda não aplicadas

As seguintes ideias permanecem apenas como propostas para homologação futura:

1. elevar `group_reply.send_group_reply` para P0;
2. deslocar `pv_reply.send_greeting` para P1, preservando prioridade alta mas abaixo do gatilho de grupo;
3. reduzir o intervalo global do Writer de 20 s para uma faixa inicial de 10–12 s;
4. manter repost e follow-ups em classes mais baixas;
5. limitar a criação de ações redundantes quando já existir uma resposta de grupo pendente para o mesmo grupo.

Nenhum desses cinco pontos está aprovado como decisão arquitetural neste documento.

## Critérios de homologação

Antes de qualquer promoção adicional, observar em produção:

- incidência de `FLOOD_WAIT`;
- incidência de `SLOWMODE_WAIT`;
- quantidade de respostas de grupo enviadas;
- quantidade de gatilhos descartados por cooldown;
- tempo real entre gatilho no grupo e resposta;
- quantidade de usuários que migram do grupo para PV após a resposta;
- tamanho e idade do backlog da Outbox;
- impacto sobre o tempo de primeira resposta no PV.

## Relação com documentação existente

Esta discussão não substitui:

- `ADR-005-cadencia-sazonal-e-arbitro-grupos.md`;
- `PR35-PRIORITY-SCHEDULER.md`.

A ADR-005 continua definindo o árbitro local entre atendimento reativo e repost. O PR35 continua definindo a política vigente do scheduler prioritário. Este arquivo apenas registra uma linha de investigação operacional.

## Invariantes

Continuam obrigatórios:

- uma única sessão Telegram USER;
- uma única Outbox;
- um único Writer;
- nenhuma segunda fila paralela de envio;
- Telegram Effects como único gateway de mutação;
- `available_at` continua sendo gate absoluto;
- `FLOOD_WAIT` e `SLOWMODE` continuam soberanos;
- sem alteração de `USER_SESSION_STRING`, API ID, API HASH, token ou segredos;
- sem migração destrutiva;
- qualquer ajuste deve ser reversível e homologado por etapas.

## Próximo passo possível

Manter os tempos 25–45 s / 60–90 s em observação. Só depois, com evidência de produção, decidir se vale testar prioridade de grupo em P0 ou reduzir o intervalo global do Writer.
