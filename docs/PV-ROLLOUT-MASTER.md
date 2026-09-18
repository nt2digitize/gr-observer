# Plano Mestre — Atendimento PV

- **Projeto:** `nt2digitize/gr-observer`
- **Data-base:** 2026-09-17
- **Produção:** `release/pr16-production` @ `2a2e3cdf7194e292776bf44f1c7174fa905b9b40`
- **Railway:** `radar-gr-observer` em `SUCCESS` no mesmo commit
- **Auto Deploy Railway:** desligado; merge não significa deploy
- **Objetivo:** uma única conversa linear por lead, mantendo uma sessão USER, uma Outbox e um Writer.

## 1. Autoridade

A conversa linear será a única autoridade de progressão do PV.

- quadros/fases = contexto visual;
- balão = conteúdo + tempo antes do envio + esperar resposta sim/não;
- espera humana = somente a lane daquele lead;
- Duas Telas e Live = capacidades chamadas pela linha, não motores paralelos;
- membership = fato passivo;
- opt-out, supressão, pacing, FloodWait e idempotência = guardrails globais.

Contrato detalhado: `docs/PV-LINEAR-AUTHORITY.md`.

## 2. Invariantes

- uma sessão Telegram USER;
- um `Observer.user_runtime`;
- uma Outbox;
- um `SafeOutboxWriter`;
- um `TrafficGovernor` global;
- `pv_reply` como uma única costela;
- nenhuma espera humana bloqueia Writer/fila global;
- mutação USER somente por `módulo -> outbox_actions -> Writer -> Effects -> Telegram`;
- efeito externo idempotente;
- banco aditivo e rollback sem apagar histórico;
- sem segundo client, Writer, Outbox, worker, fila ou serviço.

## 3. Estado das PRs

### #65 — `reminder_link`

**MERGEADA E EM PRODUÇÃO.**

Commit: `2a2e3cdf7194e292776bf44f1c7174fa905b9b40`.

Remove o reenvio condicional standalone. Dados históricos permanecem. O runtime pós-#65 não deve recriar `reminder.link`.

### #66 — conversa linear

**BANCADA PRONTA SOBRE A PRODUÇÃO PÓS-#65.**

Head atual: `b86b66e469ec57ffc6071c0b5e7733c9df63437f`.

- mergeável;
- CI verde;
- `PV_LINEAR_FLOW_ENABLED` OFF por padrão;
- schema aditivo;
- uma linha global de balões;
- quadros somente visuais;
- texto/link/mídia Telegram;
- espera por resposta por lane;
- ADR-004 impede espera antes do link inicial;
- opt-out permanece global;
- quando a flag está ON, sequenciadores legados ficam em quarentena;
- quando OFF, o runtime atual permanece disponível para rollback;
- não restaura `reminder.link` removido pela #65.

A flag NÃO deve ser ativada ainda. Duas Telas/Live precisam voltar como capacidades controladas pela linha antes do cutover real.

### #67 — membership passivo

**REANCORADA SOBRE A PRODUÇÃO PÓS-#65.**

Head atual: `4623e115280b9e6cd3d397cc4967051799a4c1e4`.

- mergeável;
- CI verde;
- mesmo cliente USER;
- zero envio Telegram;
- sem Writer/Outbox/worker novo;
- somente leads conhecidos;
- join/left/rejoin persistidos;
- duplicidade idempotente;
- evento atrasado não regressa estado;
- convite privado bruto e identidade do ator não são persistidos;
- grupo de prévia pode ser reconhecido por invite presente ou `link_targets.target_chat_id` já conhecido.

### #68 — fatos de entrega do link

**EMPILHADA SOBRE #66.**

Objetivo: ler `link_sent_at` do journal `telegram_effects` somente após efeito `succeeded`, sem criar nova fonte de verdade.

- read-only;
- nenhuma tabela/coluna nova;
- nenhum worker/polling/chamada Telegram;
- correlaciona com `link_targets.target_chat_id` quando disponível.

## 4. Legado encerrado

PRs #40 e #56–#61 permanecem encerradas como superadas, com histórico preservado.

Código/ideia antiga só pode voltar reconstruída sobre a arquitetura atual.

## 5. Próxima sequência

1. promover #66 somente com novo CTA;
2. deploy manual do commit correto no Railway, porque Auto Deploy está desligado;
3. manter `PV_LINEAR_FLOW_ENABLED=OFF`;
4. conferir logs e arquitetura;
5. depois promover #67 em shadow/passivo;
6. depois #68, se ainda fizer sentido separada após integração;
7. reincorporar Duas Telas como capacidade da linha;
8. reincorporar Live como capacidade/evento da linha;
9. adicionar variantes/respostas determinísticas sem segundo motor;
10. homologar cutover virtual e só então pedir ativação controlada da linha.

## 6. Protocolo de promoção

Para cada camada:

`CI verde -> PR mergeável -> CTA -> merge -> Deploy Latest Commit no Railway -> confirmar commit -> SUCCESS -> logs -> próxima camada`.

Sem CTA novo, não:

- mergear/deployar;
- ativar flag;
- alterar variável/segredo;
- regenerar sessão;
- fazer migração destrutiva;
- executar teste Telegram real;
- criar segundo runtime/client/Writer/Outbox/fila/serviço.

## 7. Critério final

A migração termina quando:

- somente a linha escolhe o próximo balão;
- regras antigas não avançam jornada;
- Duas Telas/Live são capacidades;
- membership é somente fato;
- opt-out e guardrails continuam independentes;
- legado substituído pode ser removido;
- CI comprova uma sessão USER, uma Outbox e um Writer.
