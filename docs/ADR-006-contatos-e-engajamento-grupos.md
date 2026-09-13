# ADR-006 — Contatos duráveis e proteção de engajamento

- **Status:** proposto/implementado nesta branch
- **Data:** 2026-09-13
- **Escopo:** `nt2digitize/gr-observer`
- **Depende de:** ADR-001, ADR-003, ADR-004 e ADR-005

## Contexto

Algumas pessoas que interagem com a conta não conseguem iniciar um novo PV enquanto não estiverem salvas como contato. Ao mesmo tempo, contatos conhecidos apenas pela agenda da conta Telegram seriam difíceis de recuperar operacionalmente se a StringSession precisasse ser regenerada ou se a conta operadora fosse substituída.

Também existe valor em preservar temporariamente uma publicação do loop que esteja gerando conversa: apagar imediatamente uma mensagem respondida pode quebrar o contexto visível do grupo.

## Decisão

A capacidade de contatos é infraestrutura compartilhada pelas costelas existentes `pv_reply` e `group_reply`; ela **não** cria nova costela, sessão, worker ou Writer. Cada costela continua dona das próprias intenções de Outbox.

São introduzidos dois níveis de estado:

1. `contact_ledger`: memória operacional do projeto sobre usuários observados, sem conteúdo de mensagem e sem descoberta/inferência de telefone;
2. `contact_account_state`: estado do vínculo daquele usuário com uma conta operadora específica.

Toda mutação Telegram continua atravessando a Outbox única, o Writer serial e `TelegramEffects`.

## Salvamento automático no PV

Quando `PV_AUTO_SAVE_CONTACTS=true`, toda mensagem privada recebida de conta elegível gera uma intenção idempotente `ensure_contact_saved`, independente da etapa atual da jornada PV.

- bot, conta apagada, suporte e `777000` continuam excluídos;
- se o remetente já vier marcado pelo Telegram como contato, apenas o ledger é reconciliado;
- o telefone não é exigido nem inferido;
- `add_phone_privacy_exception` permanece desligado: a automação não compartilha nosso telefone para completar a operação;
- uma falha conclusiva não gera confirmação falsa;
- um resultado externo incerto segue a semântica de `review` da ADR-003.

## Fluxo ADD nos grupos

Quando `GROUP_ADD_CONTACT_FLOW=true`, o fluxo só nasce se houver contexto dirigido à conta:

- resposta direta a uma mensagem nossa; ou
- menção explícita ao nosso `@username`;

mais uma intenção compatível, como `add`, `me adiciona`, `salva`, `tô de ban` ou equivalente aprovado.

A sequência é durável:

`interação → Outbox com atraso estável de 25–35 s → salvar contato → confirmar efeito → responder à mensagem`

A resposta aprovada nesta implantação é:

`já add, chama lá`

A confirmação só é enviada depois de `AddContact` ter sido concluído ou o ledger provar que o usuário já estava salvo. Se a adição falhar, a resposta não é enviada.

## Proteção de publicação engajada

Quando `GROUP_ENGAGEMENT_PROTECTION=true`, uma publicação corrente do loop que receba resposta direta pode ser protegida contra exclusão. Uma menção explícita pode proteger a publicação corrente do loop quando o contexto não contém `reply_to`.

A proteção padrão dura 24 horas e é renovável a partir da interação mais recente. A versão da proteção impede que uma limpeza antiga apague uma publicação cuja janela tenha sido renovada.

O loop continua podendo criar uma publicação nova. Se a anterior estiver protegida, ela não é apagada naquele repost. Quando a proteção expira e a publicação já não é a corrente, uma intenção de limpeza atravessa Outbox + Writer + `TelegramEffects`.

Mensagens reativas comuns do atendimento não são automaticamente convertidas em publicações do loop nem entram nessa rotina de limpeza.

## Continuidade entre sessão e conta

Regenerar a StringSession da **mesma conta** não apaga o ledger nem o estado de contato do projeto.

Uma **conta Telegram diferente** recebe outro `account_user_id`. O ledger antigo permanece como memória histórica, mas não autoriza importação em massa. A nova conta só adiciona automaticamente um usuário quando houver nova interação válida dentro de um fluxo autorizado. Um `user_id` histórico isolado não é tratado como autorização nem como garantia de resolubilidade pela nova conta.

## Privacidade e persistência

O ledger pode guardar:

- `user_id`;
- `username` quando disponível;
- nome de exibição;
- timestamps de primeira/última observação;
- origem operacional (grupo/PV e IDs de referência);
- estado de contato por conta operadora.

O corpo de mensagens privadas não é persistido por esta função. Nenhuma tentativa de descobrir telefone é feita.

## Kill switches e rollout

As três novas capacidades nascem desligadas:

- `PV_AUTO_SAVE_CONTACTS=false`
- `GROUP_ADD_CONTACT_FLOW=false`
- `GROUP_ENGAGEMENT_PROTECTION=false`

Ações de salvar contato/ADD ainda pendentes obedecem ao kill switch quando alcançam o Writer. A limpeza de uma proteção já criada continua autorizada mesmo se a criação de novas proteções for desligada, evitando deixar mensagens antigas permanentemente retidas.

## Invariantes preservados

- uma única sessão Telegram de usuário;
- um único Writer;
- uma única Outbox ativa;
- composição explícita no monólito;
- nenhuma chamada direta entre costelas;
- chaves/effects idempotentes;
- FloodWait X+5;
- pacing global inalterado;
- `review` somente para resultado externo realmente ambíguo;
- falha de um usuário não bloqueia outras lanes/ações prontas.

## Homologação

A entrada em produção é progressiva:

1. deploy com as três flags desligadas;
2. ativar apenas autosalvamento PV e validar com conta de teste;
3. ativar ADD apenas em grupo autorizado/teste;
4. testar proteção com janela curta antes de restaurar 24 h;
5. ampliar somente após confirmar ausência de duplicidade, FloodWait anormal e confirmações falsas.
