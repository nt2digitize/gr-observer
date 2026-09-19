# Diagnosticador PV Read-Only

## Objetivo

Fornecer um raio-X operacional de uma única lane de Atendimento PV sem usar o
Telegram e sem alterar estado de produção.

A ferramenta existe para responder, com evidência persistida:

- em que etapa o lead está;
- se a lane está ativa, aguardando resposta, concluída ou parada;
- qual é a próxima etapa configurada;
- se existe ação na Outbox e quando ela fica disponível;
- se houve efeito Telegram concluído, falho ou ambíguo;
- estado de Live, Duas Telas e pertencimento ao grupo de prévia.

## Limite arquitetural

O diagnosticador **não é uma costela do monólito** e não participa da execução
do atendimento. O código fica versionado no mesmo repositório apenas para
controle de versão, revisão e CI.

Ele não possui:

- `TelegramClient`;
- `USER_SESSION_STRING`;
- API ID/API HASH do Telegram;
- `SafeOutboxWriter`;
- `TrafficGovernor`;
- capacidade de enviar, apagar, encaminhar ou clicar no Telegram;
- endpoint público.

## Segurança

`scripts/pv_readonly_diagnostic.py` abre PostgreSQL com
`default_transaction_read_only=on` e executa o snapshot dentro de uma transação
`readonly=True`.

O contrato da ferramenta é SELECT-only. Não são retornados textos de mensagens,
links de convite, segredos, credenciais ou payloads completos.

A ferramenta deve ser executada como job efêmero/on-demand e encerrar após
imprimir o relatório. Não precisa ficar residente e não deve receber tráfego
externo.

## Uso operacional

```bash
python scripts/pv_readonly_diagnostic.py <telegram_user_id>
```

Requer somente `DATABASE_URL` no ambiente do job auxiliar.

O resultado é JSON sanitizado com:

- `session`;
- `next_step`;
- `outbox`;
- `effects`;
- `live`;
- `two_screens`;
- `membership`.

## Regra de operação

A ferramenta serve para diagnóstico e homologação. Nenhuma correção de estado é
feita por ela. Se o snapshot apontar lane inconsistente, a correção deve seguir
o fluxo normal de branch, testes, PR e rollout controlado.
