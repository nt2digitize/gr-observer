# GROUPS OPERATING SPEC — Operação integrada dos grupos

- **Status:** aprovado para implementação; ainda não implementado por este documento
- **Data:** 2026-09-16
- **Escopo:** `nt2digitize/gr-observer`
- **Tipo:** especificação operacional consolidada
- **Base normativa:** ADR-005 e ADR-006

## 1. Objetivo

Este documento consolida o comportamento operacional do Radar em grupos sem reescrever a arquitetura já definida.

O objetivo de produto é manter **presença contínua e controlada no grupo**, coordenando em uma única linha do tempo:

- publicação e republicação do texto de visibilidade;
- respostas reativas a perguntas/gatilhos;
- menções dirigidas à conta;
- fluxo `ADD`;
- grupos temporariamente fechados para postagem;
- mensagens removidas por administradores/bots/modo noturno;
- recuperação após reabertura;
- estado e manejo pelo painel.

A regra central é:

> **visibilidade contínua primeiro; cadência e atendimento coordenados em seguida, sem gerar rajadas nem fila represada.**

## 2. Relação com ADR-005 e ADR-006

Este documento **não substitui integralmente** ADR-005 nem ADR-006.

### ADR-005 continua sendo a autoridade para

- cálculo da cadência adaptativa;
- atividade recente, sazonalidade e baseline;
- persistência de ciclo;
- prioridade local de atendimento reativo sobre repost;
- Outbox, Writer e idempotência do loop.

### ADR-006 continua sendo a autoridade para

- salvamento de contatos;
- fluxo `ADD`;
- contexto por reply/menção;
- proteção de publicação engajada;
- ledger de contatos;
- kill switches relacionados.

### Este documento acrescenta ou especializa

- bootstrap do primeiro ciclo de grupo novo;
- detecção e recuperação de `post sumiu`;
- troca segura entre postagem antiga e nova;
- comportamento durante modo noturno/fechamento do grupo;
- reabertura com saudação genérica;
- anti-travamento de `repost_pending`;
- painel operacional consolidado;
- regras de convivência entre marketing e atendimento.

Quando houver conflito pontual, a cláusula específica deste documento prevalece **somente naquele comportamento explicitamente descrito**, sem revogar as demais decisões dos ADRs.

## 3. Invariantes arquiteturais

A implementação deve preservar obrigatoriamente:

- uma única sessão Telegram de usuário;
- uma única Outbox ativa;
- um único Writer;
- nenhuma nova sessão, cliente Telegram ou userbot;
- nenhum worker paralelo para contornar o Writer;
- efeitos Telegram somente pela infraestrutura existente;
- idempotência das intenções;
- pacing/FloodWait global existente;
- nenhum represamento de mensagens humanas aguardando resposta;
- restart não pode produzir rajada de ações acumuladas.

## 4. Estado operacional mínimo por grupo

Além do estado já existente, o Radar deve conseguir distinguir conceitualmente:

- `ACTIVE`: existe uma publicação corrente conhecida;
- `VERIFYING_NEW`: nova publicação enviada e aguardando estabilidade;
- `MISSING`: a publicação registrada não existe mais no chat;
- `CLOSED`: o grupo está temporariamente sem condição de postagem;
- `RECOVERY_WAIT`: reabertura/atividade detectada e recuperação aguardando atraso;
- `REPOST_PENDING`: repost aprovado e ainda não concluído;
- `PAUSED`: operação pausada pelo operador.

A implementação pode usar colunas, enum ou combinação de flags, desde que esses estados sejam observáveis e não ambíguos.

## 5. Grupo novo — bootstrap do primeiro ciclo

### 5.1 Primeira publicação

A primeira publicação continua sendo manual e passa a ser o `current_message_id` de referência.

### 5.2 Segunda publicação

Para grupo novo, a segunda publicação deve usar regra determinística igual para todos:

- **mínimo de 15 minutos desde a primeira publicação**; e
- **mínimo de 10 mensagens humanas novas**.

As duas condições precisam ser satisfeitas.

Esse bootstrap evita que um grupo sem histórico gere um primeiro plano excessivamente influenciado por amostragem insuficiente.

### 5.3 Depois da segunda publicação

Após a segunda publicação ser concluída com sucesso, o grupo entra na cadência adaptativa definida pela ADR-005.

## 6. Princípio de presença contínua

O Radar não deve remover voluntariamente a publicação antiga antes de confirmar que a substituta está estável no chat.

Fluxo obrigatório:

`publicação antiga ativa → envia nova → verifica estabilidade → confirma nova → remove antiga → promove nova a corrente`

Isso substitui apenas o comportamento operacional de troca imediata entre mensagens; não altera a fórmula de cadência da ADR-005.

## 7. Critério de estabilidade da nova publicação

Uma publicação nova só pode substituir definitivamente a anterior quando:

1. o Telegram confirmar o envio e retornar `message_id`;
2. transcorrer uma janela de estabilidade configurável de **3 a 5 minutos**;
3. a mensagem continuar existente no chat ao final da janela;
4. não houver evidência de que o grupo está fechado/sem condição de manter a publicação.

Se a nova mensagem desaparecer durante a janela:

- a anterior permanece como referência operacional quando ainda existir;
- a nova não é promovida a `current_message_id`;
- não é gerada rajada de tentativas;
- o Radar entra no fluxo de recuperação apropriado.

## 8. `Post sumiu`

`Post sumiu` significa que o banco mantém um `current_message_id`, mas a mensagem correspondente já não está disponível no chat.

O texto/template permanece preservado. O problema é de **referência visível**, não de perda do conteúdo.

### 8.1 Detecção

A implementação deve combinar, quando tecnicamente disponível:

- evento de mensagem apagada recebido do Telegram; e
- verificação de existência antes de troca/repost/recuperação.

A ausência confirmada da mensagem corrente deve marcar o grupo como `MISSING`.

### 8.2 Regra de recuperação

Quando `MISSING`:

- não criar múltiplos reposts;
- não acumular dívida de mensagens;
- não tentar apagar novamente um `message_id` já inexistente;
- preservar template, histórico de atividade e informações de cadência;
- recuperar apenas quando houver novo sinal válido de atividade/reabertura.

## 9. Grupo fechado / modo noturno

Quando o grupo estiver temporariamente proibindo ou eliminando novas postagens:

- não insistir em reposts repetidos;
- não gerar backlog de reposts;
- não transformar centenas de mensagens observadas em centenas de ações futuras;
- manter apenas um estado de recuperação por grupo.

Se a postagem antiga continuar visível, ela deve ser preservada.

Se a postagem corrente for removida, o grupo passa para `MISSING/CLOSED` sem represamento.

## 10. Reabertura após modo noturno ou fechamento

O Radar não deve republicar imediatamente apenas porque o relógio avançou.

A recuperação requer um **sinal humano novo**.

Fluxo:

`grupo fechado → reabre → primeira mensagem humana nova → aguarda 2–4 min → executa uma única ação de reabertura/recuperação`

Mensagens adicionais recebidas durante esses 2–4 minutos:

- continuam podendo alimentar métricas de atividade;
- não criam ações adicionais de reabertura;
- não geram fila represada.

## 11. Saudação genérica de reabertura

Para grupos configurados com essa função, quando houver evidência de que estavam fechados para postagem e voltaram a operar:

- a primeira mensagem humana nova arma a saudação;
- aguardar **2–4 minutos**;
- enviar no máximo **uma saudação por ciclo de reabertura**;
- texto padrão inicial: `oi povo 👋`;
- a saudação não substitui o template comercial;
- a saudação não conta como republicação do loop;
- a saudação não cria novo ciclo de cadência.

O painel deve permitir ligar/desligar essa função e editar o texto.

## 12. Atendimento reativo, menções e ADD

Os motores existentes continuam válidos conforme ADR-005/006.

A mudança é de **coordenação operacional**, não de reescrita do motor.

### 12.1 Precedência

Quando houver colisão no mesmo grupo:

`atendimento dirigido → repost/visibilidade`

Isso inclui:

- pergunta/gatilho reconhecido;
- reply dirigido à conta;
- menção dirigida à conta;
- fluxo `ADD` elegível.

### 12.2 Comportamento

- a resposta dirigida sai primeiro;
- o repost é adiado, não cancelado;
- a contagem do ciclo não é zerada por causa do atendimento;
- o histórico de atividade não é descartado;
- várias interações durante a guarda não criam vários reposts;
- o teto de adiamento existente da ADR-005 continua como proteção contra starvation, salvo ajuste futuro explícito.

### 12.3 ADD

O fluxo `ADD` permanece regido pela ADR-006:

`interação elegível → atraso configurado → salvar contato → confirmar efeito → responder`

Nenhum ajuste desta especificação autoriza resposta falsa de contato salvo.

## 13. Arbitragem única do grupo

A implementação deve tratar publicação, recuperação e atendimento como intenções coordenadas de uma mesma linha do tempo do grupo.

O árbitro deve impedir:

- repost cair em cima de resposta dirigida;
- duas recuperações simultâneas;
- saudação de reabertura e repost comercial saírem juntos;
- múltiplas ações derivadas do mesmo evento humano;
- exclusão prematura da última publicação visível.

A arbitragem não deve criar nova fila paralela. Toda mutação continua na Outbox única e Writer único.

## 14. Anti-travamento de `repost_pending`

`repost_pending=TRUE` não pode bloquear indefinidamente o grupo.

A implementação deve reconciliar o estado quando:

- a ação correspondente falhou de forma conclusiva;
- foi movida para review e não existe execução ativa concorrente;
- não existe mais ação `pending/processing/scheduled` correspondente;
- o estado excedeu um limite operacional configurado.

A recuperação deve ser idempotente e jamais liberar um segundo repost enquanto a primeira ação ainda puder executar.

Falha não deve apagar silenciosamente o contador/histórico que levou à decisão.

## 15. Regra de não represamento

O sistema deve operar por **estado atual**, não por dívida de ações passadas.

Exemplos:

- 200 mensagens chegaram enquanto o grupo estava fechado: não gerar 200 reposts;
- 20 gatilhos surgiram enquanto um repost esperava atendimento: não gerar 20 reposts;
- grupo reabriu: apenas uma intenção de recuperação/reabertura fica ativa;
- restart: não reproduzir todas as oportunidades históricas perdidas.

## 16. Painel operacional por grupo

O painel deve responder primeiro à pergunta de negócio:

> **Minha oferta está visível agora?**

Campos mínimos:

- **Post atual:** `EXISTE / SUMIU / VERIFICANDO`;
- **Mensagem corrente:** `current_message_id`;
- **Contagem:** `inbound_count / cycle_target_messages`;
- **Tempo:** decorrido / mínimo exigido;
- **Modo do ciclo:** `BOOTSTRAP / ADAPTATIVO / RECUPERAÇÃO`;
- **Repost pendente:** `SIM / NÃO`;
- **Última postagem:** timestamp;
- **Última atividade humana:** timestamp;
- **Grupo:** `ABERTO / FECHADO / INCERTO`;
- **Recuperação:** estado e atraso restante;
- **Saudação de reabertura:** `LIGADA / DESLIGADA`;
- **Atendimento:** `ATIVO / EM GUARDA / LIVRE`;
- **ADD:** `LIGADO / DESLIGADO`.

Controles desejados:

- `Automático`;
- `Pausar`;
- `Saudação de reabertura` on/off;
- editar texto da saudação;
- visualizar cadência atual;
- visualizar motivo de espera;
- ação manual de diagnóstico/recuperação, sem burlar Outbox/Writer.

## 17. Observabilidade

O Radar deve registrar motivo explícito para não postar/repostar, por exemplo:

- `waiting_message_target`;
- `waiting_min_interval`;
- `waiting_reply_guard`;
- `waiting_new_post_stability`;
- `current_post_missing`;
- `group_closed`;
- `recovery_wait_human_signal`;
- `recovery_delay`;
- `repost_pending_active`;
- `repost_pending_reconciled`.

Isso deve permitir diagnosticar casos de centenas de mensagens sem repost sem depender de inferência manual.

## 18. Critérios mínimos de teste

Antes de ativação em produção, cobrir pelo menos:

1. primeira publicação manual cria referência válida;
2. grupo novo exige 15 min + 10 mensagens para a segunda publicação;
3. depois da segunda publicação entra no adaptativo ADR-005;
4. nova publicação não apaga a anterior antes da estabilidade;
5. nova publicação apagada durante 3–5 min mantém a anterior;
6. mensagem corrente removida externamente vira `MISSING`;
7. `MISSING` não tenta apagar ID fantasma;
8. grupo fechado não acumula reposts;
9. reabertura exige primeira mensagem humana nova;
10. reabertura espera 2–4 min e cria só uma ação;
11. saudação ocorre no máximo uma vez por reabertura;
12. resposta/menção/ADD têm precedência sobre repost;
13. atendimento não zera contagem de cadência;
14. `repost_pending` falho não congela o grupo indefinidamente;
15. `repost_pending` ainda válido não é duplicado;
16. restart não produz rajada;
17. bots e mensagens próprias continuam fora das métricas humanas;
18. todas as escritas passam pela Outbox/Writer existentes.

## 19. Ordem recomendada de implementação

### Fase A — integridade e presença

- estado `MISSING`/verificação de existência;
- troca segura com janela de estabilidade;
- não apagar ID fantasma;
- reconciliação de `repost_pending`;
- observabilidade mínima.

### Fase B — bootstrap e reabertura

- segunda publicação padrão 15 min + 10 humanas;
- detecção de fechamento/reabertura;
- recuperação por primeira mensagem humana + 2–4 min;
- saudação genérica opcional.

### Fase C — painel e arbitragem consolidada

- indicadores operacionais;
- motivo de espera;
- controles de manejo;
- validação de colisões atendimento × repost × recuperação.

## 20. Critério de conclusão

A implementação só é considerada concluída quando, para um grupo autorizado, for possível responder de forma observável e determinística:

1. existe uma publicação nossa visível agora?
2. se não existe, por quê?
3. qual é a próxima condição para publicar?
4. existe atendimento dirigido com prioridade?
5. há alguma ação pendente capaz de travar o ciclo?
6. o sistema consegue se recuperar sem rajada, sem nova sessão e sem intervenção manual comum?

Esta especificação é a camada operacional consolidada. ADR-005 e ADR-006 permanecem como fontes técnicas das regras que não foram explicitamente especializadas acima.
