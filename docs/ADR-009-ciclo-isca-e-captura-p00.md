# ADR-009 — Ciclo de Isca e captura P00 nos grupos

- **Status:** proposto nesta branch
- **Data:** 2026-09-15
- **Escopo:** `nt2digitize/gr-observer`
- **Depende de:** ADR-005, ADR-006 e PR #35
- **Supera parcialmente:** o fluxo ADD/proteção de grupo da ADR-006 quando `GROUP_CAPTURE_P00_ENABLED=true`

## Contexto

A publicação manual persistente do grupo não é um produto separado do atendimento reativo. Ela é a **isca ativa** do ciclo: fica visível enquanto a cadência adaptativa da ADR-005 decidir e serve como origem mensurável para capturar leads.

O comportamento antigo mantinha respostas reativas genéricas e um fluxo ADD separado, além de poder prolongar uma publicação engajada por até 24 horas. Isso cria três comportamentos concorrentes e pode afastar a implementação do produto original.

## Decisão

Quando `GROUP_CAPTURE_P00_ENABLED=true`, a costela `group_reply` opera como um único **Ciclo de Isca**:

`isca ativa → interação inequívoca → captura P00 → salvar contato → reply direcionada → chegada ao PV → limpeza`

A flag nasce `false`. Com ela desligada, o comportamento legado permanece disponível para rollback.

### Isca ativa

A isca é exatamente `group_repost_state.current_message_id`, isto é, a publicação gerenciada que já obedece à cadência sazonal/adaptativa da ADR-005.

Uma captura não modifica `current_message_id`, não zera o contador do ciclo, não altera o plano sazonal e não prolonga a vida da isca. A regra antiga de proteção de engajamento não é aplicada a eventos P00.

### Gatilhos de captura

Com uma isca ativa, um humano elegível é capturado quando ocorre pelo menos uma condição:

1. reply direto à mensagem da isca ativa; ou
2. menção explícita ao `@username` da conta enquanto a isca está ativa.

Reply + menção é naturalmente aceito. Proximidade visual, posição na tela ou palavras genéricas sem vínculo não são usadas como prova.

Com P00 ligado, as respostas reativas genéricas e o fluxo ADD legado ficam dormentes naquele modo. Assim não existe um terceiro produto competindo com a isca.

## Prioridade P00

A captura usa a classe `P00_GROUP_CAPTURE=-1`, acima do P0 humano do PV.

P00 muda apenas a escolha entre ações **já elegíveis**. Permanecem soberanos:

1. `available_at`;
2. Traffic Governor;
3. pacing global;
4. FloodWait do Telegram;
5. Writer serial único.

Aging de trabalho comum termina em P0 e nunca fabrica P00. A penalidade da última lane não reduz uma captura P00 a empate com P0.

## Salvamento e resposta

A intenção P00 é persistida com `available_at=NOW()` e chave determinística por `(chat_id, bait_message_id, user_id)`.

Antes de responder no grupo, o fluxo precisa confirmar que o contato foi salvo ou já estava salvo. Somente então envia uma reply à mensagem exata do lead:

- com username: `@username já add, chama lá`;
- sem username: `já add, chama lá`.

Falha ou ambiguidade ao salvar nunca produz confirmação falsa. O estado durável do contato continua registrando `save_failed` ou `review`, e a captura registra sua própria falha para diagnóstico.

## Deduplicação

`group_capture_events` possui:

- chave primária `(chat_id, bait_message_id, user_id)`;
- unicidade adicional `(chat_id, source_message_id)`.

Isso garante no máximo uma captura por pessoa em cada isca e impede duas respostas automáticas à mesma mensagem. O evento também reserva a chave do `group_reply_events` antes do matcher legado.

## Limpeza da resposta

A resposta P00 é efêmera e nunca se torna a mensagem gerenciada do grupo.

- se o usuário chegar ao PV: limpeza é enfileirada imediatamente;
- se não chegar: fallback de 45 minutos;
- somente o `reply_message_id` persistido da própria conta pode ser apagado;
- mensagem do usuário e isca nunca são apagadas pela rotina de limpeza da captura.

A chegada ao PV é registrada mesmo se acontecer na pequena janela entre enfileirar a captura e receber do Telegram o ID da reply. Quando o ID for confirmado, uma limpeza imediata é criada além do fallback, fechando essa corrida.

## Persistência e privacidade

A tabela `group_capture_events` guarda apenas IDs, username opcional, estados e timestamps. O corpo da mensagem humana não é persistido por esta função.

Toda mutação Telegram continua pelo caminho:

`group_reply → Outbox única → Writer único → TelegramEffects → Telegram USER`

O PV apenas registra a chegada e cria uma intenção de limpeza pertencente a `group_reply`; ele não apaga mensagem de grupo diretamente.

## Rollout e rollback

1. CI completo verde;
2. merge/deploy com `GROUP_CAPTURE_P00_ENABLED=false`;
3. confirmar sessão, Writer, PV e grupos saudáveis;
4. ligar a flag em um único grupo autorizado de homologação;
5. validar captura, salvamento, P00, conversão para PV e limpeza;
6. ampliar somente depois da observação.

Rollback funcional imediato: `GROUP_CAPTURE_P00_ENABLED=false`. O estado aditivo pode permanecer no banco sem interferir no modo legado. Nenhuma credencial, sessão ou migração destrutiva é necessária.
