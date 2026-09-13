# ADR-006 — Contatos duráveis e proteção de engajamento

- **Status:** proposto/implementado nesta branch
- **Data:** 2026-09-13
- **Escopo:** `nt2digitize/gr-observer`
- **Depende de:** ADR-001, ADR-003, ADR-004 e ADR-005

## Contexto

Algumas pessoas que interagem com a conta não conseguem iniciar um novo PV enquanto não estiverem salvas como contato. Ao mesmo tempo, contatos conhecidos apenas pela agenda da conta Telegram seriam difíceis de recuperar operacionalmente se a StringSession precisasse ser regenerada ou se a conta operadora fosse substituída.

Também existe valor em preservar temporariamente uma publicação do loop que esteja gerando conversa: apagar imediatamente uma mensagem respondida pode quebrar o contexto visível do grupo.

## Decisão

A capacidade de contatos é infraestrutura compartilhada pelas costelas existentes `pv_reply` e `group_reply`; ela não cria nova costela, sessão, worker ou Writer. Cada costela continua dona das próprias intenções de Outbox.

São introduzidos `contact_ledger`, memória operacional sem corpo de mensagem nem descoberta de telefone, e `contact_account_state`, estado do vínculo por conta operadora. Toda mutação Telegram continua atravessando a Outbox única, Writer serial e `TelegramEffects`.

## Salvamento automático no PV

Quando `PV_AUTO_SAVE_CONTACTS=true`, toda mensagem privada recebida de conta elegível gera intenção idempotente `ensure_contact_saved`, independente da etapa da jornada. Bots, contas apagadas, suporte e `777000` permanecem excluídos. Telefone não é exigido nem inferido e `add_phone_privacy_exception` permanece desligado. Falha conclusiva não gera confirmação falsa e resultado externo incerto segue ADR-003.

## Fluxo ADD nos grupos

Quando `GROUP_ADD_CONTACT_FLOW=true`, o fluxo exige contexto dirigido à conta — reply a mensagem nossa ou menção explícita ao `@username` — mais intenção compatível como `add`, `me adiciona`, `salva`, `tô de ban` ou equivalente aprovado.

`interação → Outbox 25–35 s → salvar contato → confirmar efeito → responder à mensagem`

A resposta é `já add, chama lá`, somente após contato confirmado ou já salvo.

## Proteção de publicação engajada

Quando `GROUP_ENGAGEMENT_PROTECTION=true`, a proteção contra exclusão exige vínculo inequívoco do Telegram: **reply direto a uma publicação do loop**. Reply a publicação antiga já protegida renova sua janela.

Uma menção solta ao `@username`, sem `reply_to`, continua válida como contexto dirigido para o fluxo ADD, mas **não é atribuída automaticamente ao post corrente do loop** e não cria proteção. Assim, uma pergunta ou menção sem vínculo comprovado não retém publicação antiga.

A proteção padrão dura 24 horas e é renovável pela interação mais recente. A versão impede que limpeza antiga apague publicação renovada. O loop pode publicar nova cópia enquanto a anterior protegida permanece. Após expirar, a antiga só é removida se já não for a corrente, via Outbox + Writer + `TelegramEffects`.

Mensagens reativas comuns do atendimento não viram publicações do loop nem entram na rotina de limpeza.

## Continuidade, privacidade e persistência

Regenerar StringSession da mesma conta não apaga ledger/estado. Conta Telegram diferente recebe outro `account_user_id`; histórico não autoriza importação em massa. O ledger pode guardar IDs, username, nome de exibição, timestamps, origem operacional e estado por conta. Corpo de PV e descoberta de telefone ficam fora.

## Kill switches e rollout

As capacidades nascem desligadas:

- `PV_AUTO_SAVE_CONTACTS=false`
- `GROUP_ADD_CONTACT_FLOW=false`
- `GROUP_ENGAGEMENT_PROTECTION=false`

Salvamento/ADD pendentes obedecem ao kill switch na execução. Limpeza de proteção já criada continua autorizada para não reter posts indefinidamente.

## Invariantes preservados

- uma sessão Telegram de usuário;
- um Writer;
- uma Outbox ativa;
- nenhuma nova costela/worker/cliente;
- efeitos idempotentes;
- FloodWait X+5 e pacing global inalterados;
- `review` apenas para efeito externo ambíguo;
- falha de uma lane não bloqueia outras.

## Homologação

1. deploy com três flags desligadas;
2. ativar autosalvamento PV isoladamente;
3. ativar ADD em grupo autorizado/teste;
4. testar proteção curta, inclusive confirmando que menção solta não protege;
5. restaurar 24 h somente após critérios GO.