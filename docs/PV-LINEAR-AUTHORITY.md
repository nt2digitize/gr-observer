# Contrato de autoridade — Atendimento PV linear

Data-base: 2026-09-17

## Estado final obrigatório

A conversa linear é a única autoridade de progressão do Atendimento PV.

Somente a linha decide:

- qual é o próximo balão;
- quando o balão é liberado;
- se a conversa continua automaticamente;
- se a lane daquele contato aguarda nova mensagem humana;
- qual conteúdo/mídia pertence àquela posição da conversa;
- quando a conversa termina ou muda de contexto.

Os quadros/fases são somente organização visual. Mover um balão entre quadros não muda a regra de execução.

## O que pode existir fora da linha

Componentes externos à linha podem proteger ou executar uma decisão, mas não escolher a próxima etapa da conversa.

Permanecem fora da linha:

- supressão/opt-out e pausa individual;
- Outbox única, Writer único e TelegramEffects;
- TrafficGovernor, pacing e FloodWait;
- idempotência e reconciliação de efeitos;
- ContactLedger;
- fatos passivos de membership de grupo;
- catálogo/referência de mídias;
- mecânicas especializadas de envio já aprovadas.

Esses componentes não podem avançar a jornada por conta própria.

## Duas Telas

Duas Telas deixa de ser um motor paralelo de jornada.

Devem ser preservados como mecânica reutilizável:

- fotos previamente cadastradas;
- slots/preferências disponíveis;
- proteção contra repetição da mesma foto;
- envio pelo mesmo Effects/Writer;
- fallback determinístico quando a regra da linha o solicitar.

Devem deixar de mandar no fluxo:

- encadear automaticamente a próxima fase global;
- criar uma jornada concorrente à conversa linear;
- decidir sozinho quando remarketing, Live ou outro bloco começa.

## Live

Live deixa de ser uma plataforma de conversa paralela.

O subsistema pode manter fatos do evento, destino e consentimento, mas qualquer fala de PV associada a Live deve ser inserida/executada sob autoridade da conversa linear ou de um evento explicitamente aceito pela linha.

Live não pode reiniciar ou substituir a jornada do contato.

## Membership de grupo

Membership é camada factual passiva.

Ela pode informar estados como:

- link enviado / aguardando entrada;
- entrou;
- saiu;
- reentrou.

Ela nunca envia mensagem, nunca escolhe o próximo balão e nunca cria uma segunda fila.

## Autoridades legadas a aposentar

A auditoria identificou regras de progressão antigas em:

1. `Storage.accept_pv_message` — classifica inbound e altera `stage`/agenda ações;
2. `PvJourneyStore` — agenda link e inicia Duas Telas após o link;
3. `PvMessageRuntimeMixin._complete_sequence` — encadeia greeting/link/followup/weekly/Live/Duas Telas;
4. `PvPhotoFlowStore` — além da mecânica de seleção, agenda continuações próprias;
5. handlers `action_send_*` legados de `pv_reply` — validam estados antigos e avançam a máquina antiga;
6. estados/cadências `following_up`, `weekly`, `live_*` e `pv_two_screens_sessions` quando usados como autoridade de progressão.

Esses componentes não devem ser apagados antes de suas funções úteis terem sido absorvidas pela linha. Durante a transição eles são legado controlado, não arquitetura final.

## Sequência de aposentadoria

1. homologar o motor linear com feature flag OFF por padrão;
2. homologar membership em shadow;
3. mover a mecânica de Duas Telas para chamadas controladas pela linha;
4. mover falas/continuidade de Live para autoridade da linha;
5. implementar respostas determinísticas/variantes sem transformar classificadores em um segundo motor;
6. no cutover, neutralizar ações antigas pendentes para que não concorram com a linha;
7. após homologação, remover handlers, continuations, estados e documentação que ficaram sem função;
8. remover a feature flag temporária quando o motor linear for a única implementação suportada.

## Regra de espera

`wait_for_reply` pertence ao balão, não ao quadro.

Esperar significa pausar somente a lane lógica daquele contato. Nunca segura o Writer ou a fila global.

Enquanto ADR-004 estiver vigente, nenhum balão anterior ao envio do link inicial pode bloquear a chegada desse link esperando resposta humana. Se essa regra de negócio mudar, ADR-004 deve ser formalmente substituída; não deve existir contradição silenciosa entre linha, código e ADR.

## Critério de conclusão

A migração termina quando uma inspeção do runtime comprovar que, com o motor linear ativo:

- nenhuma máquina antiga escolhe a próxima mensagem;
- nenhuma continuação antiga cria uma segunda progressão;
- Duas Telas e Live são chamadas como capacidades, não como chefes de fluxo;
- ações legadas pendentes não conseguem interferir na nova conversa;
- uma sessão USER, uma Outbox e um Writer permanecem intactos;
- CI e testes de regressão cobrem o contrato acima.
