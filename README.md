# GR Observer — monólito modular

Um único processo administra uma única sessão de usuário do Telegram e liga
funções independentes (“costelas”) em uma coluna central. Hoje existem três:

1. **Radar** — observação passiva de grupos, canais, permissões, regras, bots,
   links e origem provável de conversas privadas.
2. **Atendimento PV** — recepção atrasada em duas mensagens, lembretes com
   intervalos progressivos e pergunta semanal condicionada à resposta.
3. **Testar BOTSON** — homologação ativa, limitada por allowlist, da jornada de
   acesso, botões seguros, conversas, saída e reentrada.

O painel de controle continua disponível mesmo quando as três funções estão
desligadas. O comando de produção permanece `python app.py`.

## Estrutura

```text
app.py                         entrada compatível com Railway
gr_observer/
  application.py              coluna central e sessão única
  catalog.py                  módulos, comandos e textos com IDs estáveis
  storage.py / schema.py      estado, Inbox, Outbox, execuções e efeitos
  outbox.py                   único Writer de ações ativas
  panel.py                    bot administrativo
  modules/
    radar.py                  costela passiva
    pv_reply.py               conversa privada e agenda semanal
    botson.py                 comandos e relatórios de homologação
    botson_engine.py          jornada de teste allowlisted
```

O Radar não recebe o Writer e não contém chamadas para enviar mensagens,
clicar, entrar ou sair. Atendimento PV e BOTSON recebem um portão de efeitos e
não abrem outra `TelegramClient`. Assim as funções compartilham infraestrutura,
mas não compartilham regra de negócio.

## Comandos de texto

O dicionário `COMMANDS` em `gr_observer/catalog.py` é a fonte única. A ordem é
determinada pelo campo `order`; cada operação tem ID estável e aliases. No
privado do bot de controle, apenas `ADMIN_USER_ID` pode usar:

- `painel`, `/start` ou `/observador` — painel;
- `status` ou `/status` — estado real;
- `funcoes` ou `/funcoes` — funções na ordem do catálogo;
- `ligar radar` ou `/ligar`;
- `desligar radar` ou `/desligar`;
- `ligar atendimento` ou `/ligar_atendimento`;
- `desligar atendimento` ou `/desligar_atendimento`;
- `ver mensagens pv` ou `/mensagens_pv`;
- `ligar botson`;
- `desligar botson`;
- `testar botson` ou `/testar_botson` — coloca o teste na fila e entrega o
  relatório no privado da conta controladora.

Para preservar o runner anterior, a conta controladora também pode mandar
`testar` diretamente no privado da conta de usuário. Depois do relatório:

```text
1
1 acesso
1 jornada
1 conversas
1 evidencias
1 tecnico
1 retestar
```

Essa gramática está no dicionário `BOTSON_DETAILS`, também ordenado e validado
contra aliases duplicados pelos testes.

## Dual Write

O banco e o Telegram não participam da mesma transação. O monólito trata isso
em três camadas:

1. **Inbox + Outbox:** o evento recebido e a intenção de agir são gravados na
   mesma transação PostgreSQL. Eventos repetidos não criam outra intenção.
2. **Writer único:** somente um consumidor serial executa ações ativas usando a
   sessão compartilhada.
3. **Diário de efeitos:** cada envio, clique, entrada ou saída tem chave
   determinística. Envios de texto usam `random_id` estável do Telegram.
   Entrada/saída podem ser reconciliadas pelo estado de membro. Um efeito
   interrompido e não reconciliável vai para `review`; nunca é repetido às
   cegas.

O resultado completo do teste e a intenção de entregar o relatório também são
gravados na mesma transação. Isso evita “teste concluído no banco, mas relatório
esquecido” após uma queda.

As tabelas aditivas são `module_control`, `inbox_events`, `outbox_actions`,
`module_runs`, `telegram_effects` e `pv_reply_contacts`. A Outbox aceita
`available_at`, portanto atrasos e lembretes sobrevivem a reinícios. As tabelas
antigas e `observer_control` continuam compatíveis.

## Radar

O comportamento original foi preservado:

- inventaria grupos e canais já acessíveis;
- registra permissões de texto e mídia sem interpretar prévia de link como
  autorização de divulgação;
- coleta trechos candidatos a regras, bots vistos e links;
- relaciona menção/resposta em grupo com um PV posterior por sete dias;
- cria rascunho de resposta, sem enviá-lo;
- usa postagem manual bem-sucedida como evidência de permissão.

Limites deliberados do Radar:

- não entra em links ou grupos;
- não envia nem responde pela conta;
- não clica em botões;
- não lista todos os membros;
- não afirma que uma origem provável é definitiva.

Mensagens destinadas a membros devem continuar com linguagem nativa de chat.
O texto preservado do rascunho é: `pera aí q vou ver o link certo p vc`.

## Atendimento PV

A função nasce desligada e exige `PV_PREVIEW_LINK` no Railway. O convite
privado não fica no repositório público. Os textos ficam no dicionário
`CAMPAIGNS`, em `gr_observer/catalog.py`.

Fluxo:

1. primeiro PV recebido de uma conta que o Telegram não marca como bot: após
   60 segundos, envia `Oi 😊 Tudo bem? Quer ver a esposa?`;
2. depois de qualquer resposta, envia o convite da prévia após 60 segundos;
3. após o convite, pergunta se gostou/já entrou e inclui o link. Os intervalos
   crescem aproximadamente de 1 até 7 dias: 23–25 h, 47–49 h, …, 167–169 h;
4. atingido o limite, envia somente a pergunta amistosa uma vez por semana,
   sem link;
5. resposta positiva encerra em silêncio; resposta negativa recebe o link;
6. `parar`, `não quero` e equivalentes encerram a sequência.

O limite de sete dias vale para o intervalo entre contatos, não para a duração
total da fase progressiva. Com os sete ciclos padrão, ela ocupa cerca de 28
dias; depois disso, o intervalo permanece semanal.

O texto recebido não é persistido pela função: ficam apenas ID do evento,
contato, etapa e classificação mínima da resposta. Inbox/Outbox impedem que o
mesmo update crie duas mensagens. A classificação “pessoa real” significa
somente que o Telegram não marcou a conta como bot, excluindo também contas
apagadas, suporte e o serviço `777000`; não é prova de identidade humana.

Configuração padrão:

```text
PV_PREVIEW_LINK=<preencher somente no Railway>
PV_REPLY_DELAY_SECONDS=60
PV_FOLLOWUP_MIN_HOURS=23
PV_FOLLOWUP_MAX_HOURS=25
PV_FOLLOWUP_MAX_CYCLES=7
PV_WEEKLY_INTERVAL_HOURS=168
```

## Testar BOTSON

A função nasce desligada. Para ligá-la, são obrigatórios:

- `BOTSON_PREVIEW_ALLOWLIST`;
- `BOTSON_CONTROLLER_ID`, ou `BOTSON_PAIR_CODE` para pareamento;
- a mesma `USER_SESSION_STRING` exclusiva usada pela coluna central.

O teste mantém as salvaguardas do runner:

- só opera nas prévias da allowlist;
- bloqueia compra, PIX, assinatura, cancelamento e ações de moderação;
- não abre URLs externas;
- clica apenas callbacks classificados como navegação segura;
- limita profundidade e quantidade de cliques;
- termina testando saída, recebimento do link real de recuperação no PV e
  reentrada/pedido de entrada;
- persiste relatório e histórico de efeitos no PostgreSQL.

## Instalação local

1. Copie `.env.example` para `.env` e preencha o painel/PostgreSQL.
2. Instale `pip install -r requirements.txt`.
3. Execute `python app.py`.
4. Abra `/observador` no privado do bot de controle.

No Railway, mantenha uma réplica e `restartPolicyType = NEVER`. O Dockerfile
copia `app.py` e todo o pacote `gr_observer`.

## Sessão exclusiva e AuthKeyDuplicatedError

O Railway pode manter o deploy anterior vivo por alguns segundos. Uma trava
consultiva do PostgreSQL serializa a conexão: o sucessor aguarda o contêiner
anterior desconectar e só então abre a sessão. Isso evita nova concorrência
durante deploys.

Essa proteção não recupera uma chave já invalidada. Quando aparecer
`AuthKeyDuplicatedError`, gere uma nova sessão com `python generate_session.py`,
substitua `USER_SESSION_STRING` e não reutilize o mesmo conteúdo em outro
serviço, computador ou processo.

Antes de ativar **Testar BOTSON** neste monólito, desligue o serviço standalone
que usa a mesma conta. Duas aplicações diferentes não podem compartilhar a
StringSession.

## Verificação

```bash
python -m unittest -v
python -m py_compile app.py gr_observer/*.py gr_observer/modules/*.py
git diff --check
```

Os testes são offline: validam os contratos, o catálogo, a passividade do
Radar, o bloqueio de botões perigosos, a trava de sessão e as regras de
idempotência. Não autenticam credenciais reais nem executam ações no Telegram.

Veja também:

- [Arquitetura](docs/ARCHITECTURE.md)
- [ADR da reorganização](docs/ADR-001-modular-monolith.md)
- [ADR do Atendimento PV](docs/ADR-002-atendimento-pv.md)
- [Inventário e lacunas](docs/FEATURE-GAP.md)
- [Plano de ativação e rollback](docs/CUTOVER.md)
