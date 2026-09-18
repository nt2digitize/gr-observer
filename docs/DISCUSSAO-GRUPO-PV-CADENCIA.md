# Discussão — Cadência e prioridade Grupo → PV

- **Status:** em discussão
- **Data:** 2026-09-18
- **Escopo:** `nt2digitize/gr-observer`
- **Objetivo operacional:** aumentar a conversão de interações em grupos para conversas no PV, preservando a sessão única, Outbox única, Writer único, pacing global e resposta a `FLOOD_WAIT`/`SLOWMODE`.

## Contexto

O objetivo principal desta etapa é responder a gatilhos humanos em grupos com rapidez suficiente para aumentar a chance de o usuário migrar para o PV. Depois que o usuário já chamou no PV, a conversão principal da etapa Grupo → PV já aconteceu.

A discussão surgiu porque tempos conservadores e o intervalo global do Writer podem aumentar a latência entre a fala no grupo e a resposta automática. Ao mesmo tempo, a redução desses tempos não pode ignorar os mecanismos de segurança já existentes.

## Estado atual confirmado no código de produção

O código da branch `release/pr16-production` mantém hoje:

- `USER_ACTION_MIN_INTERVAL_SECONDS = 20.0` como intervalo global padrão do Writer;
- `USER_WRITE_MIN_INTERVAL_SECONDS = 3.0` como pacing técnico entre writes;
- `GROUP_REPLY_MIN_DELAY_SECONDS` com default de `70` s;
- `GROUP_REPLY_MAX_DELAY_SECONDS` com default de `90` s;
- `GROUP_REPLY_MIN_COOLDOWN_SECONDS` com default de `95` s;
- `GROUP_REPLY_MAX_COOLDOWN_SECONDS` com default de `110` s.

Os quatro tempos de grupo podem ser sobrescritos por variáveis de ambiente. Em 2026-09-18 foi discutido/testado o alvo operacional `25–45 s` de delay e `60–90 s` de cooldown, porém a evidência disponível nesta revisão não é suficiente para afirmar que esses valores estão efetivamente ativos no runtime. Portanto, eles permanecem registrados como **alvo de homologação**, não como fato confirmado de produção.

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
3. tornar o intervalo global do Writer configurável e testar redução controlada de `20 s` para `14 s`;
4. manter repost e follow-ups em classes mais baixas;
5. limitar a criação de ações redundantes quando já existir uma resposta de grupo pendente para o mesmo grupo.

Nenhum desses cinco pontos está aprovado como decisão arquitetural neste documento.

## Estudo — regulagem do Atendimento de Grupos

Esta proposta trata somente do **Atendimento de Grupos**. Os tempos e fluxos do PV ficam fora deste controlador, exceto pelo fato de todos os efeitos Telegram continuarem compartilhando a mesma Outbox e o mesmo Writer global.

A regulagem proposta separa quatro relógios distintos, evitando misturar causas e efeitos:

### 1. Writer global

Controla a cadência máxima de ações automáticas da sessão Telegram USER.

Estado atual confirmado no código: `20 s` por ação não-core.

Proposta:

- tornar o valor configurável pelo painel do Radar;
- manter um valor padrão conservador no código;
- permitir modo fixo e modo adaptativo;
- manter `FLOOD_WAIT` soberano;
- manter o pacing técnico de writes separado do pacing entre ações.

Exemplo de superfície no painel:

- Cadência atual: `20 s`;
- atalhos: `20 | 16 | 14 | 12`;
- opção `Definir manualmente`;
- opção `Voltar ao padrão`;
- exibição do valor anterior e do último ajuste.

### 2. Delay de resposta reativa no grupo

Controla quanto tempo o módulo espera entre detectar um gatilho humano e tornar a resposta elegível para envio.

Variáveis existentes:

- `GROUP_REPLY_MIN_DELAY_SECONDS`;
- `GROUP_REPLY_MAX_DELAY_SECONDS`.

Esse delay não deve ser confundido com o Writer. A resposta pode ficar elegível após, por exemplo, 25 s e ainda esperar o Writer global se houver outra ação na frente.

Proposta:

- tornar mínimo e máximo editáveis pelo painel;
- preservar uma faixa, em vez de um número exato, para evitar comportamento rígido;
- aplicar limites inferiores e superiores configurados;
- não reduzir automaticamente esse delay apenas porque a fila cresceu.

### 3. Cooldown por grupo

Controla o intervalo entre respostas reativas sucessivas dentro do mesmo grupo.

Variáveis existentes:

- `GROUP_REPLY_MIN_COOLDOWN_SECONDS`;
- `GROUP_REPLY_MAX_COOLDOWN_SECONDS`.

Objetivo:

- evitar rajadas em um único grupo;
- impedir que vários gatilhos próximos monopolizem a Outbox;
- ajudar a manter comportamento distribuído entre grupos.

Proposta:

- tornar a faixa editável pelo painel;
- manter um piso mínimo;
- considerar aumento temporário após sinais de limitação associados ao grupo;
- estudar a regra de no máximo uma resposta reativa pendente por grupo.

### 4. Repost / visibilidade

O repost permanece um mecanismo separado, orientado por atividade e sazonalidade do grupo. Ele não deve usar o mesmo controlador do atendimento reativo.

Princípio:

- resposta reativa = conversa humana acionada por gatilho;
- repost = manutenção de visibilidade;
- ambos compartilham Outbox/Writer, mas possuem objetivos e cadências diferentes.

## Estudo — controlador adaptativo de cadência

A hipótese é adotar um controlador com **freio rápido e aceleração lenta**, inspirado em controle de congestionamento, sem tentar inferir um “limite oficial” inexistente.

### Princípios

1. `FLOOD_WAIT_X` sempre prevalece: o sistema espera o período informado pelo Telegram, acrescido do buffer de segurança já existente quando aplicável.
2. `SLOWMODE_WAIT_X` é tratado como limitação específica do contexto/grupo e não como autorização para acelerar outras rotas.
3. O tamanho da fila indica demanda, mas **não é prova de capacidade disponível**; portanto, backlog alto não deve causar aceleração automática.
4. Aceleração deve ser gradual e somente após uma janela limpa e estável.
5. Desaceleração pode ser mais rápida quando aparecem sinais de limitação.
6. Todo ajuste deve respeitar pisos, tetos e rollback.

### Estados sugeridos

**VERDE**

- nenhuma limitação recente relevante;
- cadência estável;
- após uma janela longa e limpa, pode reduzir o intervalo em `1 s`, respeitando o piso.

**AMARELO**

- ocorrência recente de `FLOOD_WAIT`, `SLOWMODE_WAIT` ou degradação de entrega;
- congelar qualquer aceleração;
- manter ou aumentar moderadamente o intervalo.

**VERMELHO**

- limitações repetidas, wait elevado ou sequência de eventos restritivos;
- aumentar o intervalo de forma mais forte;
- suspender novas tentativas de aceleração por uma janela de resfriamento;
- preservar a fila e não descartar ações válidas.

### Exemplo conceitual

Valor inicial escolhido pelo operador: `14 s`.

- operação estável por janela longa → `14 → 13 s`;
- novo período estável → `13 → 12 s`;
- `FLOOD_WAIT` → obedecer o wait e subir, por exemplo, `12 → 16 s`;
- `FLOOD_WAIT` repetido → `16 → 20 s`;
- estabilidade posterior → redução lenta, `20 → 19 → 18...`.

Os números acima são apenas exemplo de política de controle. Não constituem limites oficiais do Telegram.

### Contrapesos obrigatórios

- piso e teto configuráveis;
- valor padrão conservador;
- limite de quanto pode acelerar por ciclo;
- limite de quanto pode desacelerar por evento;
- janela mínima de estabilidade antes de acelerar;
- congelamento temporário após limitação;
- histórico do valor anterior;
- botão de retorno imediato ao padrão;
- registro de cada ajuste e motivo;
- possibilidade de desligar o modo adaptativo e voltar ao modo fixo.

## Painel proposto — Ritmo do Atendimento de Grupos

Exemplo conceitual:

```text
⚙️ ATENDIMENTO DE GRUPOS — RITMO

Writer global: 14 s
Modo: ADAPTATIVO
Faixa permitida: 10–30 s
Estado: VERDE

Resposta do grupo: 25–45 s
Cooldown por grupo: 60–90 s
Repost: adaptativo independente

Último FloodWait: nenhum recente
Último ajuste: 15 → 14 s
Último valor estável: 16 s

[ Writer ] [ Resposta ] [ Cooldown ]
[ Automático/Fixo ] [ Voltar ao padrão ]
```

A interface deve deixar claro que:

- Writer = gargalo global da sessão;
- Delay = espera antes de uma resposta de grupo ficar elegível;
- Cooldown = proteção local do mesmo grupo;
- Repost = cadência independente de visibilidade.

## Critérios de homologação

Antes de qualquer promoção adicional, observar em produção:

- incidência de `FLOOD_WAIT`;
- incidência de `SLOWMODE_WAIT`;
- duração dos waits recebidos;
- quantidade de respostas de grupo enviadas;
- quantidade de gatilhos descartados por cooldown;
- tempo real entre gatilho no grupo e resposta;
- tamanho e idade do backlog da Outbox;
- distribuição de ações entre grupos;
- frequência e direção dos autoajustes;
- estabilidade após cada mudança;
- quantidade de respostas redundantes pendentes por grupo.

Para este estudo, métricas de PV não devem governar o autoajuste do Atendimento de Grupos.

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

Se este estudo for aprovado futuramente para implementação, a sequência sugerida é:

1. tornar o intervalo do Writer configurável sem mudar seu default de 20 s;
2. expor configuração manual no painel;
3. homologar modo fixo em 14 s;
4. adicionar telemetria e histórico de ajustes;
5. só então experimentar o controlador adaptativo;
6. depois integrar, separadamente, delay e cooldown do Atendimento de Grupos ao mesmo painel;
7. manter o PV fora da lógica de autoajuste deste módulo.
