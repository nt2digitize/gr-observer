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

A ADR-009 acrescenta P00 sem alterar as proteções originais do scheduler:

| Classe | Uso |
|---|---|
| P00 | captura humana da isca ativa em grupo autorizado |
| P0 | humano aguardando resposta no PV agora |
| P1 | reativo legado de grupo e continuação PV ativa |
| P2 | automação conversacional normal |
| P3 | follow-up, semanal e campanha diferida |
| P4 | repost, limpeza e manutenção |

A classificação é por `module_id + action_type`; não existe regra simplista de
"PV sempre ganha de Grupo". P00 existe somente para `group_capture_contact_reply`
e somente quando a captura já passou pelo gate de `available_at`.

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

Para trabalho comum, a ação pronta recebe uma prioridade efetiva:

`prioridade efetiva = prioridade envelhecida + penalidade da última lane`

A cada 180 segundos de espera *depois de `available_at`*, uma ação comum ganha
uma classe, **no máximo até P0**. Aging nunca fabrica P00. Quando trabalho comum
chega à mesma prioridade efetiva, a ação mais antiga vence. Isso impede
starvation.

A lane atendida no turno anterior recebe penalidade de uma classe durante a
próxima escolha. Isso permite intercalar pessoas/conversas sem paralelizar o
Writer. Depois de outro turno, a penalidade desaparece.

P00 é uma exceção semântica estreita: uma captura P00 pronta permanece P00 e não
recebe a penalidade da última lane, para não virar empate artificial com P0. Isso
não antecipa `available_at`, não ignora pacing, Governor ou FloodWait e não cria
paralelismo.

## Persistência

`outbox_scheduler_state` guarda apenas:

- última lane atendida;
- último action id;
- horário do último claim.

A tabela é aditiva. Não há `DROP`, `TRUNCATE` ou migração destrutiva.

O claim ocorre dentro de uma transação PostgreSQL, respeita módulos ligados,
`status='pending'`, `available_at<=NOW()` e usa `FOR UPDATE ... SKIP LOCKED`.

## Bancada

`scripts/bench_priority_scheduler.py` reproduz a política histórica usada para
gerar a ordenação do scheduler. O cenário principal cria 1, 5, 10 e 20 pedidos
de ADD legado em grupo, depois um contato chama no PV. Há uma segunda matriz com
follow-ups/reposts antigos já pendentes.

A ADR-009 adiciona regressões específicas separadas para provar que P00 vence P0
somente quando ambos já estão prontos, que uma ação futura nunca ultrapassa
`available_at` e que aging comum para em P0.

O modelo usa 20 segundos por turno do Writer, igual ao intervalo mínimo atual.
Ele mede espera de fila, não latência de rede do Telegram nem tempo cosmético de
digitação. O atraso configurado do PV permanece separado e não é reduzido pela
PR #35.

## Gate de promoção

A PR original só podia ser promovida quando:

1. CI completo estivesse verde;
2. testes de arquitetura confirmassem uma sessão/Writer;
3. P0 pronto tivesse overhead de fila de no máximo um turno no burst de bancada;
4. P4 provasse envelhecimento até conseguir turno;
5. nenhuma ação futura ultrapassasse `available_at`;
6. links contextuais provassem P0 apenas com proveniência humana recente;
7. a #34, base arquitetural desta PR, estivesse consolidada antes do deploy.

A extensão P00 da ADR-009 acrescenta os gates: P00 real precisa vencer P0 no
seletor Python e no SQL de produção; trabalho comum nunca pode envelhecer para
P00; e P00 nunca pode furar `available_at`.

## Registro de promoção

A PR #34 foi consolidada em `release/pr16-production` pelo merge
`b10859fa2273ddd0bc2b1dc41446416297dd13f9`. A PR #35 foi então retargetada
para essa release e validada novamente pelo CI antes do merge final
`62295d1f79024d937450a5ba387768b5b3853e01`.

A extensão P00 permanece governada pela ADR-009 e por sua própria flag de
rollout; este documento apenas mantém o contrato do scheduler coerente com a
política vigente.
