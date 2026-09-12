# Cadência da Outbox

## Objetivo

Evitar rajadas de escrita pela sessão de usuário quando o serviço reconecta, reinicia ou encontra uma fila atrasada.

## Premissas

O Telegram não publica um limite fixo e universal de envio para contas de usuário. O limite efetivo é dinâmico e pode variar por método, destinatário, histórico da conta e estado antispam. Quando o servidor devolve `FLOOD_WAIT_X`, `X` é a espera obrigatória antes de repetir aquela operação.

Os limites públicos da Bot API não são usados como alvo para este userbot.

## Política implementada

A outbox continua sendo serial e única para a sessão de usuário.

- após iniciar/reconectar: 45 s de aquecimento antes de consumir a fila;
- entre ações não-core da sessão de usuário: mínimo de 20 s;
- entre efeitos individuais de escrita da sessão de usuário: mínimo de 3 s;
- em `FLOOD_WAIT_X`: o Writer único para, espera `X + 5 s` e só então repete a mesma operação;
- ações `core` do bot de controle não usam a cadência da sessão de usuário;
- nenhuma ação atrasada é descartada apenas por estar vencida: a fila é drenada gradualmente.

Esses números são margens operacionais conservadoras, não uma garantia contra limitação. Qualquer `FLOOD_WAIT` recebido do Telegram prevalece sobre os intervalos locais.

## Segurança operacional

A cadência existe para evitar burst e respeitar rate limiting, não para contornar moderação ou antispam. Mensagens privadas automatizadas devem permanecer restritas aos fluxos autorizados do produto e respeitar opt-out e contato iniciado pelo usuário quando aplicável.
