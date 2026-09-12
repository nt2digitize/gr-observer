# ADR-001 — Sessão única em monólito modular

- **Status:** implementado; composição explícita revisada em 2026-09-11
- **Data original:** 2026-09-09
- **Escopo alterado:** somente `nt2digitize/gr-observer`
- **Referência funcional lida:** `nt2digitize/telegram-inspector-lab`

## Contexto

O Radar e o runner BOTSON estavam em processos separados. Ambos podiam abrir a
mesma conta do Telegram, enquanto o Railway pode sobrepor contêiner antigo e
novo durante deploy. O resultado observado foi `AuthKeyDuplicatedError` e a
invalidação da StringSession.

O runner também concentrava conexão, pareamento, comandos, jornada ativa e
relatório. Isso tornava difícil controlar funções separadamente e criava
janelas de Dual Write entre estado local e ações no Telegram.

Depois da primeira implementação do monólito, o Atendimento de Grupos foi
adicionado por monkey patch em importação (`group_integration.py` alterava
catálogo, painel e `Observer.setup`). Isso preservou a função, mas contrariava a
regra do próprio desenho: a coluna central deve conhecer e registrar todas as
costelas explicitamente.

## Decisão

Adotar um único deploy, uma única conexão de usuário e módulos internos
ordenados:

- `radar`: comportamento passivo existente;
- `pv_reply`: recepção privada e agenda progressiva/semanal;
- `group_reply`: respostas/republicações em grupos autorizados;
- `botson`: comportamento de homologação allowlisted existente;
- `application`: dona do ciclo de vida e da sessão;
- `outbox`: única saída ativa;
- `catalog`: dicionários de módulos, comandos, detalhes e textos estáveis.

A composição é explícita: `Observer.setup` registra cada costela (diretamente ou
por função chamada por ele). Importações não podem mutar `MODULES`, `COMMANDS`,
`ControlPanel` ou `Observer.setup`.

O estado de cada função é independente em `module_control`. `/ligar` e
`/desligar` continuam sendo aliases do Radar. BOTSON e os atendimentos nascem
desligados.

O fluxo ativo adota Transactional Inbox/Outbox, Writer serial e diário de
efeitos. Efeitos ambíguos são bloqueados em `review`.

## Preservação funcional

### Radar

O ato final permanece o mesmo: observar e persistir evidências, sem agir pela
conta. As tabelas originais, o painel, os comandos e o rascunho existente foram
preservados.

### Atendimento de Grupos

O ato final permanece o mesmo: observar somente grupos autorizados, responder
pedidos novos com atraso/cooldown e manter o texto manual reaparecendo após dez
mensagens novas. A allowlist é uma fonte inicial; uma postagem manual bem-sucedida
também pode autorizar dinamicamente o grupo e definir/substituir o modelo.

As intenções ativas permanecem na Outbox e são executadas pelo Writer único.
O módulo não abre uma segunda `TelegramClient`.

### Testar BOTSON

O ato final permanece o mesmo: testar apenas alvos autorizados, registrar a
jornada, sair, receber o caminho real de recuperação, tentar reentrar e entregar
relatório por texto. A classificação de botões perigosos e os limites de
cliques/profundidade foram preservados.

O pareamento usa PostgreSQL como fonte autoritativa. Na primeira execução, o
módulo ainda pode ler o marcador antigo em Mensagens Salvas e migrá-lo.

### Atendimento PV

O ato final é uma sequência iniciada somente por mensagem recebida no
privado: pergunta inicial, link após a próxima resposta, lembretes com
intervalos crescentes até sete dias e depois pergunta semanal. Resposta
positiva encerra sem resposta; negativa recebe o link; opt-out encerra tudo.
Contas marcadas como bot, apagadas, de suporte e `777000` são ignoradas.

## Dados afetados

As tabelas existentes não são removidas ou renomeadas. O monólito mantém, entre
outras, as tabelas:

- `module_control`;
- `inbox_events`;
- `outbox_actions`;
- `module_runs`;
- `telegram_effects`;
- `pv_reply_contacts`;
- `group_reply_events`;
- `group_repost_state`.

`outbox_actions.available_at` mantém ações futuras no banco.

`observer_control` continua sendo atualizado junto com o estado do Radar para
compatibilidade de rollback.

Não são movidas credenciais, sessões ou dados de outros bancos.

## Sessão única

A trava consultiva PostgreSQL serializa o pequeno overlap entre deploy antigo e
novo **quando ambos usam o mesmo banco**. Ela não protege contra um segundo
serviço, notebook, Termux ou outro processo que receba a mesma StringSession.

`AuthKeyDuplicatedError` significa que a chave afetada deve ser tratada como
invalidada. O processo correto é encerrar qualquer consumidor concorrente,
gerar uma nova StringSession e cadastrá-la apenas no monólito. Reiniciar ou
recolar o mesmo valor não é recuperação.

O painel só mostra `Sessão única: online` depois da autorização da conta ser
confirmada; criar um `TelegramClient` ainda não significa sessão online.

## Consequências

Benefícios:

- uma sessão não é aberta por duas costelas;
- funções têm liga/desliga e motivo independentes;
- todos os módulos e comandos têm IDs e ordem explícitos no catálogo;
- não há monkey patch de composição;
- relatório sobrevive a reinício;
- ações externas têm rastreabilidade e proteção contra repetição cega;
- rotação local de sessão arquiva a credencial anterior sem sobrescrevê-la.

Custos:

- PostgreSQL é dependência do runtime ativo;
- ação em `review` exige inspeção/reconciliação;
- desligar uma costela ativa durante execução reinicia com segurança a sessão
  compartilhada e pode interromper a execução em curso;
- autenticação e 2FA continuam exigindo ação humana local.

## Ativação

Não fazer cutover enquanto a StringSession estiver inválida. Antes de habilitar
qualquer costela, qualquer standalone que use a mesma conta deve estar parado e
uma sessão nova e exclusiva deve existir apenas no monólito. Consulte
`CUTOVER.md`.

## Rollback

1. Desligar as quatro funções no painel.
2. Confirmar `Sessão única: offline`.
3. Reimplantar o commit de rollback definido para a release.
4. Manter as tabelas aditivas; código anterior pode ignorá-las.
5. Se um standalone voltar a ser usado, ele deve receber outra sessão exclusiva;
   nunca copiar a StringSession do monólito.
