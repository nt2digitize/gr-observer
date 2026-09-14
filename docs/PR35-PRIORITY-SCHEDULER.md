# PR #35 — Scheduler prioritário do Writer único

## Objetivo

Reduzir a espera de uma pessoa que acabou de interagir, sem aumentar a taxa de
envio Telegram e sem criar outra sessão, Outbox ou Writer.

## Invariantes

- uma única sessão Telegram USER;
- uma única Outbox persistente;
- um único `SafeOutboxWriter`;
- `available_at` continua sendo gate absoluto: ação futura nunca antecipa;
- pacing de escrita e `TrafficGovernor` continuam soberanos;
- FloodWait nunca é ultrapassado por prioridade;
- nenhum módulo envia Telegram fora do gateway de efeitos;
- scheduler não armazena conteúdo de mensagem privada.

## Classes

| Classe | Uso |
|---|---|
| P0 | humano aguardando resposta no PV agora |
| P1 | resposta/ADD humano em grupo e continuação PV ativa |
| P2 | automação conversacional normal |
| P3 | follow-up, semanal e campanha diferida |
| P4 | repost, limpeza e manutenção |

A classificação é por `module_id + action_type`; não existe regra simplista de
"PV sempre ganha de Grupo".

### P0 contextual

`send_live_link` e `send_reminder_link` não são P0 apenas pelo nome da ação.
Elas viram P0 somente quando a própria chave determinística do Outbox comprova
que nasceram do ramo de mensagem humana (`pv_reply:live-link:` ou
`pv_reply:conditional-link:`) e a ação tem no máximo 300 segundos desde a
criação. Depois dessa janela, voltam a P2 e continuam sujeitas ao aging normal.

Esses dois prefixos são criados apenas dentro de `Storage.accept_pv_message`.
Um teste estrutural conta as ocorrências no arquivo e falha se essa proveniência
for reutilizada fora do processamento de inbound PV.

## Fairness

A ação pronta recebe uma prioridade efetiva:

`prioridade efetiva = prioridade envelhecida + penalidade da última lane`

A cada 180 segundos de espera *depois de `available_at`*, a ação ganha uma
classe, até P0. Quando chega à mesma prioridade efetiva de trabalho novo, a mais
antiga vence. Isso impede starvation.

A lane atendida no turno anterior recebe penalidade de uma classe durante a
próxima escolha. Isso permite intercalar pessoas/conversas sem paralelizar o
Writer. Depois de outro turno, a penalidade desaparece.

## Persistência

`outbox_scheduler_state` guarda apenas:

- última lane atendida;
- último action id;
- horário do último claim.

A tabela é aditiva. Não há `DROP`, `TRUNCATE` ou migração destrutiva.

O claim ocorre dentro de uma transação PostgreSQL, respeita módulos ligados,
`status='pending'`, `available_at<=NOW()` e usa `FOR UPDATE ... SKIP LOCKED`.

## Bancada

`scripts/bench_priority_scheduler.py` reproduz a mesma política Python usada para
gerar a ordenação do scheduler. O cenário principal cria 1, 5, 10 e 20 pedidos
de ADD em grupo, depois um contato chama no PV. Há uma segunda matriz com
follow-ups/reposts antigos já pendentes.

O modelo usa 20 segundos por turno do Writer, igual ao intervalo mínimo atual.
Ele mede espera de fila, não latência de rede do Telegram nem tempo cosmético de
digitação. O atraso configurado do PV permanece separado e não é reduzido pela
PR #35.

## Gate de promoção

A PR só pode ser promovida quando:

1. CI completo estiver verde;
2. testes de arquitetura confirmarem uma sessão/Writer;
3. P0 pronto tiver overhead de fila de no máximo um turno no burst de bancada;
4. P4 provar envelhecimento até conseguir turno;
5. nenhuma ação futura ultrapassar `available_at`;
6. links contextuais provarem P0 apenas com proveniência humana recente;
7. a #34, base arquitetural desta PR, estiver consolidada antes do deploy.

## Registro de promoção

A PR #34 foi consolidada em `release/pr16-production` pelo merge
`b10859fa2273ddd0bc2b1dc41446416297dd13f9`. A PR #35 foi então retargetada
para essa release. Este commit existe para forçar nova validação CI sobre a base
definitiva antes do merge e do deploy em produção.
