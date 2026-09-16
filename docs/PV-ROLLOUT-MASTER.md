# Plano Mestre — Atendimento PV / Rollout Fino

- **Projeto:** `nt2digitize/gr-observer`
- **Documento vivo:** atualizar após cada promoção relevante
- **Data-base:** 2026-09-16
- **Objetivo:** registrar o que está rodando, a arquitetura obrigatória, a pilha de PRs, os testes, o caminho de promoção e o destino final do Atendimento PV.

## 1. Papel deste documento

Este é o documento operacional principal do rollout fino do Atendimento PV. Ele não substitui ADRs arquiteturais; ele consolida o estado real da produção e organiza a sequência de implantação.

Em caso de conflito:

1. invariantes de segurança/arquitetura em `ARCHITECTURE.md`, `ARCHITECTURE-CLEANLINESS.md`, ADR-003, ADR-004 e ADR-008 prevalecem;
2. o código efetivamente presente em `release/pr16-production` define o estado de produção;
3. este documento define a ordem operacional do rollout e deve ser atualizado quando a produção mudar.

## 2. Arquitetura que não pode ser quebrada

O rollout inteiro preserva:

- uma única sessão Telegram USER;
- um único `Observer.user_runtime`;
- uma única Outbox persistente;
- um único `SafeOutboxWriter` para mutações USER;
- um único `TrafficGovernor`/pacing global;
- `pv_reply` como uma única costela arquitetural;
- cada `user_id` como lane lógica independente dentro de `pv_reply`;
- `available_at`, Governor, pacing e FloodWait soberanos sobre qualquer prioridade de negócio;
- nenhuma espera por resposta humana segurando Writer ou fila global;
- nenhuma repetição cega de efeito externo ambíguo;
- nenhuma segunda sessão USER, segundo Writer, segunda Outbox ou serviço paralelo para estas features;
- banco somente aditivo durante este rollout;
- rollback de feature sem apagar dados antigos;
- Radar de produção passivo e dirigido por updates.

Caminho de escrita obrigatório:

```text
módulo -> outbox_actions -> SafeOutboxWriter -> SafeTelegramEffects -> Telegram
```

## 3. Estado real de produção na data-base

Branch de produção:

```text
release/pr16-production
```

Commit atual após a camada 2:

```text
45e200fe8aff12ceb93086cb5c10b1be7185d91b
```

Railway:

```text
project: telegram-inspector-lab
service: radar-gr-observer
deployment: abc312cd-f3b7-4b1e-81c0-3f7f97a7e4f1
status: SUCCESS
```

Estado funcional confirmado após deploy:

- painel online;
- sessão USER retomada com handoff seguro entre deploys;
- Radar conectado em modo passivo;
- Atendimento PV conectado;
- Atendimento de Grupos conectado;
- BOTSON continua desligado por padrão;
- sem novo `Traceback` associado às camadas #52/#53;
- warnings de invites expirados de grupos são anteriores e não pertencem a este rollout.

## 4. O que já foi implantado

### Camada 1 — PR #52 — Menu PV

**Estado:** mergeada, implantada e aprovada manualmente pelo operador.

Entrega:

```text
💬 ATENDIMENTO PV

👋 Entrada
📸 Duas telas / Homenagem
⚡ Eventos
♻️ Remarketing
💭 Conversa livre
⏸ Pausar chat de uma pessoa
⚙️ Configuração
```

Regras preservadas:

- mudança visual/navegação;
- callbacks existentes reutilizados;
- nenhum runtime, estado, tempo, Outbox, Writer, sessão ou variável alterados.

Teste manual realizado:

- operador abriu Atendimento PV;
- navegou nas seções;
- aprovou a camada;
- observações menores poderão ser ajustadas posteriormente em produção sem misturar escopo de outras PRs.

### Camada 2 — PR #53 — Schema aditivo de balões

**Estado:** mergeada e implantada.

Mudança de banco:

```sql
ALTER TABLE pv_message_steps ADD COLUMN IF NOT EXISTS media_source_peer BIGINT;
ALTER TABLE pv_message_steps ADD COLUMN IF NOT EXISTS media_source_message_id BIGINT;
ALTER TABLE pv_message_steps ADD COLUMN IF NOT EXISTS media_kind TEXT;
```

Regras:

- zero `DROP`;
- zero exclusão de dados;
- `content`, `kind`, IDs, `step_key`, ordem e tempos permanecem;
- `CHECK(kind IN ('text','link'))` permanece;
- nenhum binário de mídia no PostgreSQL;
- os novos campos podem permanecer em rollback porque são opcionais e inertes para código antigo.

## 5. Pilha original de desenvolvimento

A pilha foi construída em PRs pequenas e testadas:

| Ordem original | PR | Tema | Estado atual de promoção |
|---:|---:|---|---|
| 1 | #52 | Menu PV | **PRODUÇÃO / aprovado** |
| 2 | #53 | Schema aditivo | **PRODUÇÃO** |
| 3 | #54 | Executor genérico de balões com mídia | aguardando promoção |
| 4 | #55 | Editor multimídia | aguardando promoção |
| 5 | #56 | Entrada | aguardando promoção |
| 6 | #57 | Duas telas / Homenagem | **ADIADA / NÃO PROMOVER** |
| 7 | #58 | Eventos | aguardando reancoragem sem #57 |
| 8 | #59 | Remarketing | aguardando reancoragem sem #57 |
| 9 | #60 | Conversa Livre | aguardando reancoragem sem #57 |
| 10 | #61 | Contextos e Memória shadow | aguardando reancoragem sem #57 |

Todas as PRs #52–#61 tiveram CI verde em sua pilha de construção. Antes de promoção, cada PR deve ser novamente alinhada à produção atual e revalidada.

## 6. Decisão importante: #57 Homenagem fica fora deste rollout

A PR #57 **não será mergeada nesta fase**.

Motivo:

- ADR-004 explicitamente colocou captura de mídia recebida de fãs fora daquela implantação;
- a feature é válida como evolução futura, mas merece decisão/documentação própria antes de produção;
- não é necessário para atingir o objetivo imediato do editor multimídia, Entrada, Eventos, Remarketing e Conversa Livre shadow.

### Regra de preservação futura

Não perder a feature de vista. Backlog futuro deve incluir:

```text
Homenagem pós-Duas-Telas
- aceitar foto/vídeo/GIF somente após mídia Duas Telas realmente entregue;
- janela limitada;
- não armazenar binário;
- não autorizar reutilização automática em marketing;
- manter uma Outbox/Writer/sessão;
- registrar decisão arquitetural antes de merge;
- homologar isoladamente.
```

### Consequência para a pilha

As PRs #58–#61 foram originalmente construídas sobre a #57. Portanto:

> **É proibido promover #58–#61 diretamente da pilha atual.**

Antes de cada promoção, a cadeia deve ser reancorada/reconstruída para excluir integralmente a #57.

Nova linha desejada:

```text
produção -> #54 -> #55 -> #56 -> #58 -> #59 -> #60 -> #61
```

A #57 permanece separada para futuro.

## 7. Camadas restantes do rollout atual

### Camada 3 — PR #54 — Executor genérico de balões

Objetivo:

- enviar balão com referência Telegram de mídia;
- preservar o caminho legado para texto/link;
- usar somente `TelegramEffects.send_catalogued_media` e efeitos de texto existentes;
- não criar action type, fila, Writer, cliente ou worker.

Comportamento:

```text
sem mídia -> caminho legado exatamente como antes
com mídia -> efeito de mídia existente
com mídia + texto -> mídia + texto companheiro, ambos idempotentes
```

Testes técnicos antes do teste humano:

- CI verde contra produção atual;
- texto antigo continua no `super()._send_row`;
- mídia passa por Effects, nunca Telethon direto;
- chaves de efeito determinísticas;
- deploy SUCCESS;
- ausência de traceback novo;
- sessão/Writer/Outbox permanecem únicos.

Teste manual do operador:

- contato de teste recebe um balão antigo de texto;
- contato de teste recebe uma mídia de teste;
- confirmar ausência de duplicação, destinatário incorreto ou travamento de outros PVs.

### Camada 4 — PR #55 — Editor multimídia

Objetivo:

- `📎 Conteúdo` aceita texto/link/foto/vídeo/GIF;
- mídia fica no Telegram e banco guarda somente referência;
- editar conteúdo não recria o balão.

Invariantes:

- mesmo `id`;
- mesmo `step_key`;
- mesma posição;
- mesmo tempo;
- entrada vazia/cancelamento não apaga conteúdo;
- texto novo limpa referência antiga de mídia;
- sem download/upload de arquivo pelo editor.

Teste manual:

1. abrir um balão de teste;
2. trocar texto por foto/vídeo;
3. salvar;
4. editar novamente;
5. voltar para texto;
6. confirmar que o balão permaneceu na mesma posição e tempo.

### Camada 5 — PR #56 — Entrada

Objetivo visual:

```text
👋 ENTRADA

👋 Saudação
🔗 Primeiro acesso / link
💬 Pós-link
```

Regra:

- somente organização dos blocos existentes `greeting`, `link`, `followup`;
- sem novo estado/action type/tabela/runtime.

Teste manual:

- conta limpa inicia PV;
- observar Saudação -> link -> pós-link;
- validar texto, ordem e sensação do timing;
- confirmar que silêncio humano não trava a lane nem a fila global conforme ADR-004.

### Camada 6 do rollout atual — PR #58 — Eventos

A numeração da PR permanece #58, porém operacionalmente ela passa a ser a próxima camada depois de #56 porque #57 foi adiada.

Objetivo:

```text
⚡ EVENTOS

🔴 Criar live
👂 Consentimento
📣 Convite
♻️ Remarketing do evento
🔗 Mensagem + destino
```

Limite atual:

- a categoria visual é Eventos;
- o único tipo executável nesta fase continua sendo **live**;
- não criar motor genérico de campanha nesta promoção.

Antes de promoção:

- reconstruir/reancorar a PR sobre a produção com #56, sem qualquer diff da #57;
- CI verde novamente;
- revisar que somente `live_*` existentes são reutilizados.

Teste manual:

- abrir Eventos;
- criar live de teste;
- revisar consentimento, convite, remarketing e destino;
- confirmar que a interface deixa claro onde alterar mensagem/link.

### Camada 7 do rollout atual — PR #59 — Remarketing

Objetivo:

```text
♻️ REMARKETING

📅 Diário / progressivo
🔗 Reenvio de destino
📆 Semanal
⚡ Remarketing de evento
```

Reutiliza:

- `followup`;
- `reminder_link`;
- `weekly`;
- `live_remarketing`.

Não inventar nesta fase:

- scheduler mensal;
- novo worker;
- nova tabela;
- nova fila/action type.

Teste manual:

- revisar os blocos no painel;
- confirmar textos/cadências já configurados;
- ciclos longos devem ser validados por estado/`next_followup_at`, não esperando dias em produção.

### Camada 8 do rollout atual — PR #60 — Conversa Livre

Objetivo visual:

```text
💭 CONVERSA LIVRE

🧠 Contextos e Memória
📚 Base de fatos
🗣 Respostas observadas
✅ Respostas aprovadas
👂 Listener
🤖 Automação — OFF
```

Regras desta fase:

- novas superfícies são administrativas/read-only;
- nenhuma resposta nova é colocada na Outbox pela Conversa Livre;
- não existe botão para ativar resposta automática;
- Automação permanece OFF.

Teste manual:

- navegar por todos os itens;
- confirmar organização intuitiva;
- verificar que nenhuma ação dispara resposta no PV.

### Camada 9 do rollout atual — PR #61 — Contextos e Memória shadow

Objetivo:

- expor somente leitura do MiniLearn já existente;
- mostrar contextos agregados;
- mostrar respostas humanas observadas;
- mostrar estado do Listener;
- manter fatos/aprovações/automação sem ativação.

Regras:

- não ligar Listener pelo painel;
- não criar schema pelo painel;
- não aprovar respostas automaticamente;
- não enfileirar mensagem;
- inbound bruto não vira base de fatos;
- falha do shadow não interfere na jornada PV.

Teste manual:

- conversar manualmente com contato de teste sobre temas como preço/link/live;
- operador responde manualmente;
- abrir Contextos e Memória;
- confirmar que os sinais aparecem como classificação/exemplo, sem resposta automática.

## 8. Protocolo obrigatório de promoção

Para cada camada restante:

```text
1. produção atual confirmada
2. reancorar a branch da camada sobre a produção atual
3. garantir diff exclusivo do escopo
4. CI completa verde
5. PR mergeável
6. merge isolado
7. deploy do mesmo serviço radar-gr-observer
8. confirmar commit exato no Railway
9. deployment SUCCESS
10. conferir handoff de sessão
11. inspecionar logs
12. meus testes técnicos
13. liberar UMA ação manual para o operador
14. operador responde APROVADO ou FALHOU
15. somente com APROVADO iniciar a próxima camada
```

### Se falhar

```text
parar -> preservar logs/evidências -> diagnosticar a camada atual ->
corrigir somente o escopo -> CI -> redeploy -> retestar
```

Se a correção exigir sessão, segredo, migração destrutiva, Writer/Outbox/serviço novo ou risco relevante de bloqueio Telegram, parar e pedir nova autorização.

## 9. Rollback operacional

Princípio:

- rollback de código não deve exigir apagar as colunas aditivas;
- nunca reusar efeito Telegram ambíguo cegamente;
- não alterar `review` para `pending` sem reconciliação;
- não trocar StringSession por causa de bug funcional;
- não criar serviço paralelo para “salvar” uma camada com problema.

Para mudança puramente visual:

```text
reverter commit -> deploy -> validar painel
```

Para executor/editor:

```text
reverter camada problemática -> manter schema aditivo -> deploy ->
confirmar texto/link legado saudável
```

## 10. Destino final deste ciclo

Ao concluir o rollout atual, o Atendimento PV deve continuar sendo **um único motor leve**, com painel organizado por responsabilidades:

```text
💬 Atendimento PV
├── 👋 Entrada
│   ├── Saudação
│   ├── Primeiro acesso/link
│   └── Pós-link
├── 📸 Duas telas
│   ├── convite/escolha
│   └── mídias existentes
├── ⚡ Eventos
│   └── live nesta fase
├── ♻️ Remarketing
│   ├── progressivo
│   ├── reenvio
│   ├── semanal
│   └── evento/live
├── 💭 Conversa Livre
│   ├── Contextos e Memória
│   ├── Base de fatos [futura]
│   ├── Respostas observadas
│   ├── Respostas aprovadas [futura]
│   ├── Listener shadow
│   └── Automação OFF
├── ⏸ Pausar uma pessoa
└── ⚙️ Configuração
```

O painel pode ter muitos blocos de negócio; a arquitetura continua com uma sessão, uma Outbox, um Writer e uma costela `pv_reply`.

## 11. Backlog posterior a este ciclo

Itens conscientemente fora do rollout atual:

1. **Homenagem pós-Duas-Telas (#57)** — futura, com decisão arquitetural própria;
2. **Base de Fatos operacional** — preço, pagamento, links, evento vigente, promoções;
3. **Respostas aprovadas** — curadoria explícita humana;
4. **Automação da Conversa Livre** — somente após fatos + aprovação + confiança + fail-closed;
5. **Eventos além de live** — campanha/post/novidade como dados, não novos runtimes;
6. **Cadência mensal** — somente quando houver scheduler/regra realmente definida;
7. eventual melhoria para mídia + legenda no mesmo efeito, se justificar sem ampliar risco.

## 12. Controle de mudanças deste documento

Após cada camada aprovada em produção, atualizar pelo menos:

- commit atual de `release/pr16-production`;
- deployment Railway;
- estado da PR;
- resultado técnico;
- resultado do teste manual;
- observações encontradas em produção;
- próximo passo autorizado.

Nunca registrar segredos, StringSession, API hash/token, telefone, códigos ou conteúdo sensível de credencial neste documento.

## 13. Resumo executivo

Na data-base:

```text
#52 Menu PV            -> PRODUÇÃO / APROVADO
#53 Schema aditivo     -> PRODUÇÃO / SUCCESS
#54 Executor mídia     -> PRONTO PARA REVALIDAÇÃO/PROMOÇÃO
#55 Editor multimídia  -> AGUARDA #54
#56 Entrada            -> AGUARDA #55
#57 Homenagem          -> ADIADA / FUTURO / NÃO MERGEAR
#58 Eventos            -> REANCORAR SEM #57
#59 Remarketing        -> REANCORAR SEM #57
#60 Conversa Livre     -> REANCORAR SEM #57
#61 Contextos/Memória  -> REANCORAR SEM #57
```

Próxima promoção funcional planejada: **#54 — Executor genérico de balões com mídia**, após revalidação contra a produção atual.
