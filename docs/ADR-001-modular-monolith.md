# ADR-001 — Sessão única em monólito modular

- **Status:** implementado em branch de homologação; ainda não ativado em produção
- **Data:** 2026-09-09
- **Escopo alterado:** somente `nt2digitize/gr-observer`
- **Referência funcional lida:** `nt2digitize/telegram-inspector-lab`

## Contexto

O Radar e o runner BOTSON estavam em processos separados. Ambos podem abrir a
mesma conta do Telegram, enquanto o Railway pode sobrepor contêiner antigo e
novo durante deploy. O resultado observado foi `AuthKeyDuplicatedError` e a
invalidação da StringSession.

O runner também concentrava conexão, pareamento, comandos, jornada ativa e
relatório. Isso tornava difícil controlar funções separadamente e criava
janelas de Dual Write entre estado local e ações no Telegram.

## Decisão

Adotar um único deploy, uma única conexão de usuário e módulos internos
ordenados:

- `radar`: comportamento passivo existente;
- `pv_reply`: recepção privada e agenda progressiva/semanal;
- `botson`: comportamento de homologação allowlisted existente;
- `application`: dona do ciclo de vida e da sessão;
- `outbox`: única saída ativa;
- `catalog`: dicionários de módulos, comandos, detalhes e textos estáveis.

O estado de cada função é independente em `module_control`. `/ligar` e
`/desligar` continuam sendo aliases do Radar. O BOTSON nasce desligado e não é
habilitado por migração automática.

O fluxo ativo adota Transactional Inbox/Outbox, Writer serial e diário de
efeitos. Efeitos ambíguos são bloqueados em `review`.

## Preservação funcional

### Radar

O ato final permanece o mesmo: observar e persistir evidências, sem agir pela
conta. As tabelas originais, o painel, os comandos e o rascunho existente foram
preservados.

### Testar BOTSON

O ato final permanece o mesmo: testar apenas alvos autorizados, registrar a
jornada, sair, receber o caminho real de recuperação, tentar reentrar e entregar
relatório por texto. A classificação de botões perigosos e os limites de
cliques/profundidade foram preservados.

O pareamento novo passa a ter PostgreSQL como fonte autoritativa. Na primeira
execução, o módulo ainda lê o marcador antigo em Mensagens Salvas e o migra.

### Atendimento PV

O ato final é uma sequência iniciada somente por mensagem recebida no
privado: pergunta inicial, link após a próxima resposta, lembretes com
intervalos crescentes até sete dias e depois pergunta semanal. Resposta
positiva encerra sem resposta; negativa recebe o link; opt-out encerra tudo.
Contas marcadas como bot, apagadas, de suporte e `777000` são ignoradas.

## Dados afetados

As tabelas existentes não são removidas ou renomeadas. São adicionadas:

- `module_control`;
- `inbox_events`;
- `outbox_actions`;
- `module_runs`;
- `telegram_effects`;
- `pv_reply_contacts`.

`outbox_actions` recebe a coluna aditiva `available_at` para ações futuras.

`observer_control` continua sendo atualizado junto com o estado do Radar para
compatibilidade de rollback.

Não foram movidas credenciais, sessões ou dados de outros bancos. Não houve
alteração no repositório/deploy `telegram-inspector-lab`.

## Consequências

Benefícios:

- uma sessão não é aberta por duas costelas;
- funções têm liga/desliga e motivo independentes;
- comandos têm IDs e ordem estáveis;
- relatório sobrevive a reinício;
- ações externas têm rastreabilidade e proteção contra repetição cega.

Custos:

- PostgreSQL passa a ser dependência também do runner ativo;
- ação em `review` exige inspeção/reconciliação;
- desligar uma costela ativa durante execução reinicia com segurança a sessão
  compartilhada e pode interromper a execução em curso.

## Ativação

Não fazer cutover enquanto a StringSession atual estiver inválida. Antes de
habilitar `botson` no monólito, o serviço standalone deve ser desligado e uma
sessão nova e exclusiva deve estar apenas no monólito. Consulte `CUTOVER.md`.

## Rollback

1. Desligar as três funções no painel.
2. Confirmar a desconexão da sessão de usuário.
3. Reimplantar o commit anterior `65ce874`.
4. Se necessário, restaurar `observer_control.enabled` pelo painel `/ligar`.

As tabelas novas podem permanecer: o código anterior as ignora. Nenhum `DROP`
é necessário. Se o standalone voltar a ser usado, ele deve receber outra sessão
exclusiva; nunca copiar a StringSession em dois serviços.
