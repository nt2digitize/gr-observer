# Arquitetura — espinha de peixe

## Visão geral

```mermaid
flowchart TB
    P["Bot de controle"] --> S["Coluna central\nregistro + sessão única"]
    U["Eventos da conta"] --> S
    S --> R["Costela 1\nRadar passivo"]
    S --> V["Costela 2\nAtendimento PV"]
    S --> B["Costela 3\nTestar BOTSON"]
    S --> D["PostgreSQL\nInbox + Outbox + estado"]
    D --> W["Writer único"]
    W --> T["Telegram"]
```

A coluna central possui a conexão, o ciclo de vida, a ordem dos módulos e o
despacho de eventos. Uma costela conhece apenas seu próprio caso de uso:

| Componente | Pode ler Telegram | Pode gravar banco | Pode agir no Telegram |
|---|---:|---:|---:|
| Coluna central | sim | estado operacional | somente conexão |
| Radar | sim | sim | não |
| Atendimento PV | eventos privados | via contrato | somente pelo Writer |
| Testar BOTSON | sim | via contrato | somente pelo Writer |
| Painel | não pela conta | sim | responde pelo bot de controle |
| Writer | não decide regra | diário de efeitos | sim, serialmente |

O painel é um canal administrativo. A resposta de confirmação não é estado
autoritativo: se ela falhar, `status` consulta novamente o PostgreSQL.

## Ordem e isolamento

`MODULES`, `COMMANDS` e `CAMPAIGNS`, em `gr_observer/catalog.py`, são
dicionários cuja ordem é explícita. IDs como `radar.enable`, `pv.greeting` e
`botson.run` são contratos; o texto pode ganhar aliases sem alterar a operação.

No fluxo de eventos da conta, a ordem de despacho também vem do catálogo:
BOTSON (comandos), Atendimento PV e Radar. Um comando consumido pelo BOTSON não
vira atendimento nem falsa origem no Radar. Um PV comum passa pelo Atendimento
e ainda chega ao Radar para eventual origem provável. Não existe chamada
direta de uma costela para a outra.

## Dual Write

```mermaid
sequenceDiagram
    participant TG as Telegram
    participant IN as Dispatcher
    participant DB as PostgreSQL
    participant WR as Writer
    TG->>IN: evento com chat_id + message_id
    IN->>DB: transação Inbox + intenção Outbox
    DB-->>IN: aceito, repetido ou ocupado
    WR->>DB: reserva uma ação
    WR->>TG: efeito com chave determinística
    WR->>DB: resultado / revisão
```

Propriedades:

- `inbox_events` elimina repetição do mesmo update;
- `outbox_actions.action_key` elimina repetição da mesma intenção;
- `outbox_actions.available_at` mantém atrasos e agenda semanal no banco;
- só há um `OutboxWriter` por sessão;
- `telegram_effects.effect_key` registra cada efeito antes da chamada externa;
- texto usa `random_id` determinístico aceito pela API do Telegram;
- entrada e saída reconciliam o estado de membro antes de qualquer repetição;
- callback ambíguo nunca é repetido automaticamente;
- processos interrompidos movem ações/efeitos para `review` após o novo processo
  obter a trava exclusiva da sessão;
- resultado de teste + intenção de entregar relatório são uma transação única.
- cada avanço do Atendimento PV atualiza seu estado e cria a próxima intenção
  agendada na mesma transação; o envio usa o mesmo diário de efeitos.

Isso não promete “exactly once” mágico entre PostgreSQL e Telegram. O contrato
é: deduplicar onde existe chave/idempotência, reconciliar onde o estado pode ser
lido e exigir revisão onde o efeito externo ficou ambíguo.

## Estado do Atendimento PV

```mermaid
stateDiagram-v2
    [*] --> Saudacao: primeiro PV
    Saudacao --> Espera: envia pergunta
    Espera --> Link: próxima resposta
    Link --> Progressivo: envia convite
    Progressivo --> Semanal: intervalo chega a 7 dias
    Semanal --> Encerrado: resposta positiva ou opt-out
    Semanal --> Semanal: negativa envia link / sem resposta aguarda próxima semana
```

O texto recebido não atravessa a fronteira de persistência. O módulo reduz a
resposta a `unknown`, `positive`, `negative` ou `opt_out`. Essa classificação só
controla o link e o encerramento; mensagens ambíguas não recebem resposta
automática adicional.

Referências:

- [Transactional Outbox](https://microservices.io/patterns/data/transactional-outbox.html)
- [AWS — Transactional Outbox Pattern](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/transactional-outbox.html)

## Trava de sessão não é Dual Write

`AuthKeyDuplicatedError` é outro problema: concorrência de dois processos com a
mesma StringSession. Uma trava consultiva de sessão PostgreSQL cobre o pequeno
overlap de deploys do Railway. Ela não substitui Inbox/Outbox e não permite que
um segundo serviço use a mesma chave.

## Como adicionar outra costela

1. Defina o módulo em `MODULES` com ID e ordem estáveis.
2. Defina seus comandos/aliases em `COMMANDS`.
3. Implemente `on_connect`, `on_disconnect` e, se necessário, o consumidor de
   eventos.
4. Para ação ativa, registre um tipo na Outbox e use `TelegramEffects`.
5. Não passe a outra costela como dependência.
6. Cubra comandos, isolamento, idempotência e bloqueios de segurança em testes.

Uma função que apenas lê pode ter seu próprio job interno. Uma função que envia,
clica, entra ou sai sempre atravessa Outbox + Writer.
