# ADR-004 — Jornada PV autônoma: link e primeira foto não dependem de resposta

- **Status:** proposto/implementado nesta branch
- **Data:** 2026-09-12
- **Escopo:** somente `nt2digitize/gr-observer`
- **Supera:** a regra da ADR-002 que exigia uma segunda mensagem humana para liberar o link

## Contexto

A ADR-003 estabeleceu que uma conversa privada é apenas uma lane lógica dentro
de `pv_reply` e que uma pessoa aguardando resposta não pode reter o Writer nem a
fila global. A regra antiga da ADR-002 ainda deixava a própria jornada daquela
lane parada em `awaiting_reply`, e o ramo "duas telas" podia parar novamente em
`awaiting_choice`.

O requisito operacional passa a ser mais forte: para todo contato elegível que
iniciou PV, a ausência de resposta pode alterar a preferência, mas não pode ser
pré-condição para continuar a jornada.

## Invariantes

1. Somente um PV recebido de conta elegível inicia a jornada; filtros de bot,
   conta apagada, suporte, `777000` e opt-out permanecem válidos.
2. Depois de enviar a saudação, a mesma transação de persistência avança a lane
   para `link_queued` e cria uma intenção `send_link` com `available_at`.
3. Portanto, silêncio humano não impede a entrega do link. Uma resposta que
   chegue antes pode atualizar o estado da conversa, mas não cria um segundo
   link inicial.
4. `parar`, `não quero`, `não envie` e equivalentes continuam interrompendo a
   automação. Toda ação "duas telas" confere o estado do contato antes de agir.
5. O link de prévia continua atravessando `TelegramEffects`; somente esse efeito
   permite webpage/link preview (`no_webpage=False`). O Telegram decide se o
   tipo de convite possui metadados suficientes para renderizar o card.
6. O ramo "duas telas" só nasce após a entrega do link e quando existe ao menos
   uma mídia catalogada.
7. Ao mostrar a pergunta de preferência, a aplicação abre uma janela durável de
   escolha e agenda na Outbox um fallback automático. Se a pessoa responder
   antes, sua preferência vence a corrida transacional. Se não responder, o
   fallback escolhe deterministicamente uma categoria disponível ainda não
   enviada e enfileira a primeira foto.
8. A primeira foto é, portanto, obrigatória para todo contato elegível enquanto
   existir ao menos uma mídia disponível e o Telegram aceitar a entrega. Falha
   definitiva, conta inacessível e ausência de mídia são exceções explícitas.
9. Fotos adicionais continuam dependentes de novas interações; esta ADR garante
   somente a primeira foto autônoma.
10. A foto continua com spoiler e TTL de **30 segundos** nesta implantação. A
    decisão de remover o TTL será separada, após observação do comportamento
    real.
11. Quando a mídia cadastrada precisa ser recuperada pelo cliente do painel, a
    cópia de cortesia é preparada em memória como JPEG, com lado máximo de
    **1080 px** e qualidade **80**. A referência/original cadastrada não é
    alterada. Se o formato não puder ser decodificado, o envio cai para os bytes
    originais em vez de quebrar a jornada.

## Arquitetura e pacing

Nada nesta mudança cria outro `TelegramClient`, Writer, worker ou costela.
Todas as mutações Telegram continuam passando pela Outbox única e pelo
`TelegramEffects` com chave idempotente. Permanecem inalterados:

- 45 s de aquecimento após start/reconnect;
- mínimo global de 20 s entre ações da sessão de usuário;
- mínimo de 3 s entre escritas individuais;
- `FLOOD_WAIT_X` = espera `X + 5 s` e repetição da mesma ação;
- `review` somente para resultado externo realmente ambíguo.

O fallback de foto é uma nova intenção persistida, não um `sleep` esperando a
pessoa responder. Uma resposta e o fallback concorrem pela mesma linha
`pv_two_screens_sessions` sob transação; somente um deles pode avançar
`awaiting_choice -> photo_queued`.

## Fora desta implantação

O resgate de conversas antigas/mortas não é executado aqui. Ele será uma etapa
separada, que deverá reconstruir a próxima ação a partir do estado persistido,
com janela/cadência próprias e sem repetição cega. Também não faz parte desta
ADR capturar fotos recebidas de fãs nem autorizar seu uso posterior em
marketing.
