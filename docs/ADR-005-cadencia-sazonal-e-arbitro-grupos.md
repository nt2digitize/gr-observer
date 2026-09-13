# ADR-005 — Cadência sazonal e árbitro interno do Atendimento de Grupos

- **Status:** proposto/implementado nesta branch
- **Data:** 2026-09-13
- **Escopo:** `nt2digitize/gr-observer`
- **Supera:** a regra fixa da ADR-001 que republicava o modelo após dez mensagens novas

## Contexto

O Atendimento de Grupos possui dois comportamentos legítimos dentro da mesma
costela: respostas reativas a mensagens humanas compatíveis e a republicação do
texto manual para manter visibilidade. Um contador fixo de dez mensagens não
representa grupos com ritmos muito diferentes nem a sazonalidade por horário.
Também é possível que uma resposta de atendimento e uma republicação fiquem
prontas quase ao mesmo tempo, produzindo duas escritas próximas no mesmo grupo.

A solução precisa sobreviver a restart sem recalcular arbitrariamente o próximo
ciclo e sem criar outra sessão, Writer, worker ou fila paralela.

## Decisão

Manter uma única costela `group_reply`, com dois motores internos independentes:

- **atendimento reativo**: mantém triggers, atraso, cooldown e Outbox existentes;
- **loop de visibilidade**: passa a usar um plano persistido por ciclo.

Um árbitro interno por grupo coordena apenas a proximidade das duas intenções. O
Writer continua sem decidir regra de negócio.

## Plano sazonal por ciclo

Somente mensagens humanas recebidas entram na medição. Bots e mensagens da
própria conta não contam.

A atividade é estimada como:

`atividade = 60% recente + 30% histórico do mesmo dia/horário + 10% baseline`

- **recente:** taxa aproximada dos últimos 30 minutos;
- **mesmo dia/horário:** média histórica daquele dia da semana e hora local;
- **baseline:** taxa horária dos últimos sete dias.

As contagens são agregadas em buckets de 15 minutos. Não é persistido o texto
das mensagens para calcular sazonalidade.

A atividade produz duas travas persistidas:

- `cycle_target_messages`: quantidade de novas mensagens humanas;
- `cycle_min_interval_seconds`: tempo mínimo desde a última postagem.

As duas condições precisam ser satisfeitas. Tempo sozinho nunca dispara uma
republicação.

O alvo é limitado a 2–40 mensagens e o tempo mínimo a 15 minutos–4 horas. Esses
limites são proteções operacionais, não metas de volume.

## Persistência e restart

O plano é calculado uma vez e persistido com:

- alvo de mensagens;
- intervalo mínimo;
- score de atividade;
- instante de cálculo;
- validade do plano;
- contador já observado;
- última postagem e última atividade humana.

Restart curto mantém exatamente o mesmo plano e contador.

Quando o plano vence, ele não é recalculado por relógio nem gera uma postagem.
A **próxima mensagem humana** provoca o recálculo no contexto sazonal atual,
preservando o contador acumulado. Assim, um downtime longo não reaproveita
cegamente a fotografia de atividade de horas atrás e também não dispara ao
religar sem nova atividade humana.

Depois que as duas travas são satisfeitas, `repost_pending=TRUE` e a intenção é
criada na Outbox. A partir daí a decisão está tomada e não é recalculada após
restart.

## Árbitro atendimento × loop

Atendimento reativo tem precedência local sobre o loop porque responde a uma
pessoa específica. Antes de executar um repost aprovado, o módulo verifica se
há:

- resposta reativa realmente pendente na Outbox; ou
- resposta reativa enviada recentemente no mesmo grupo.

Nesse caso, o repost não é cancelado. Uma nova intenção idempotente de repost é
agendada alguns minutos depois, mantendo `repost_pending=TRUE`.

Defaults desta implantação:

- guarda após atendimento: 180 s;
- adiamento máximo acumulado: 900 s.

O teto de adiamento evita starvation: passado esse limite desde a aprovação do
repost, ele pode seguir mesmo que o grupo continue produzindo atendimentos.

## Invariantes arquiteturais

Permanecem obrigatórios:

- uma única sessão Telegram de usuário;
- uma única Outbox ativa;
- um único Writer;
- efeitos Telegram somente por `TelegramEffects`;
- FloodWait e pacing globais inalterados;
- nenhuma chamada direta entre costelas;
- nenhuma mensagem humana em espera segura a fila;
- ações já aprovadas sobrevivem a restart;
- falha/review de um grupo não cria outro Writer nem sessão.

## Dados

A tabela `group_repost_state` recebe apenas estado operacional do ciclo. A nova
`group_activity_buckets` persiste contagens agregadas de mensagens humanas por
15 minutos; não armazena conteúdo de mensagens.

Buckets antigos podem ser descartados após a janela histórica necessária.

## Consequências

Benefícios:

- grupos rápidos e lentos deixam de compartilhar um contador artificial;
- horário e dia da semana passam a influenciar a próxima decisão;
- restart não muda o ciclo arbitrariamente;
- downtime longo não usa para sempre um plano sazonal antigo;
- respostas humanas podem sair perto de um loop quando necessário, mas o loop
  cede temporariamente quando houver colisão;
- não há realimentação por mensagens próprias.

Custos/riscos:

- mais estado persistido e consultas agregadas;
- o histórico precisa de alguns dias/semanas para ganhar qualidade; no início o
  sinal recente tem maior peso prático;
- parâmetros precisam ser observados em produção antes de qualquer afinação;
- erro no árbitro poderia atrasar demais o loop, mitigado pelo teto de adiamento.
