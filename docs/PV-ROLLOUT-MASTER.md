# Plano Mestre — Atendimento PV / estado atual

- **Projeto:** `nt2digitize/gr-observer`
- **Data-base:** 2026-09-17
- **Branch de produção:** `release/pr16-production`
- **Commit de produção auditado:** `0d87a6d12113696a5dc2d7f2f20bb272c5d7c493`
- **Objetivo atual:** conduzir o lead até o grupo de prévias, acompanhar entrada/saída e manter comunicação mínima, determinística e auditável.

## 1. Autoridade e invariantes

Em caso de conflito prevalecem, nesta ordem:

1. `docs/ARCHITECTURE.md`, `ARCHITECTURE-CLEANLINESS.md` e ADRs vigentes;
2. código efetivamente presente em `release/pr16-production`;
3. este plano operacional.

Invariantes obrigatórios:

- uma única sessão Telegram USER;
- um único `Observer.user_runtime`;
- uma única Outbox persistente;
- um único `SafeOutboxWriter` para mutações USER;
- um único pacing/FloodWait global;
- `pv_reply` permanece uma única costela arquitetural;
- cada `user_id` é uma lane lógica independente;
- nenhuma espera humana segura a fila global;
- nenhuma segunda sessão, Writer, Outbox, worker ou serviço paralelo;
- migrações somente aditivas durante este ciclo;
- rollback sem apagar histórico;
- qualquer envio Telegram continua pelo caminho `módulo -> Outbox -> Writer -> Effects -> Telegram`.

## 2. Decisão de produto atual

O Atendimento PV não terá IA/LLM nesta etapa.

A conversa será controlada por:

- sequência linear de balões;
- estados objetivos do lead;
- gatilhos determinísticos pequenos;
- respostas previamente cadastradas;
- variações aprovadas da mesma intenção;
- silêncio/falha de classificação significando não improvisar.

Prioridade de produto:

```text
lead inicia PV
-> saudação
-> link da prévia
-> observar se entrou no grupo
-> manter comunicação mínima
-> Duas Telas e Live como continuidade/engajamento
-> eventual saída do grupo vira sinal para remarketing futuro controlado
```

Preço/pagamento não são prioridade agora. Videochamada pode usar futuramente gatilho determinístico e resposta pronta, sem negociação automática.

## 3. Arquitetura da conversa linear

A conversa nova deve ter:

- uma única linha por lead;
- quadros apenas como contexto visual/fase;
- balões com ordem global;
- texto, link e mídia compatível;
- tempo configurado como espera antes do envio;
- balão podendo continuar automaticamente ou aguardar uma nova mensagem daquele lead;
- espera sempre por lane, nunca pelo Writer global.

A chegada de uma mensagem humana não autoriza texto inventado. Futuras respostas determinísticas devem consultar estado e gatilhos explícitos.

## 4. Estado de grupo do lead

O estado mínimo desejado passa a incluir fatos como:

- `link_sent` — link enviado;
- `joined` — entrada no grupo observada;
- `left` — saída observada;
- `rejoined` — nova entrada após saída;
- timestamps de primeira/última entrada e última saída;
- contadores de entrada/saída;
- grupo/chat relacionado;
- convite usado quando o Telegram fornecer essa informação.

A observação de membership é passiva: registrar fatos não cria mensagem, Outbox ou ação Telegram.

Uso futuro desses fatos:

- antes de `joined`: repertório de convite/ajuda de acesso;
- depois de `joined`: parar de insistir no link e mudar para proximidade/engajamento;
- depois de `left`: somente sinalizar elegibilidade para remarketing com cooldown e regra própria;
- nunca dizer automaticamente “vi que você saiu”.

## 5. PRs técnicas atuais

### PR #65 — remover `reminder_link`

**Estado:** manter aberta.

Razão:

- remove uma feature standalone que já não pertence ao desenho atual;
- elimina `send_reminder_link`, bloco/editor/retiming e reenvio condicional semanal;
- preserva histórico do PostgreSQL;
- CI verde;
- prepara terreno para a conversa linear sem duas lógicas concorrentes de reenvio.

### PR #66 — conversa linear com quadros de contexto

**Estado:** manter aberta; principal motor novo.

Regras:

- flag `PV_LINEAR_FLOW_ENABLED` permanece OFF por padrão;
- migração aditiva;
- runtime legado ainda não é removido nesta PR;
- `reminder_link` não entra na linha nova;
- homologação deve ocorrer com conta controlada antes de ativação real.

Antes de promoção, reancorar/revalidar contra a produção resultante das PRs menores que forem promovidas antes dela.

### PR #67 — tracker passivo de membership

**Estado:** manter aberta; CI verde.

Escopo:

- observa entrada/saída de leads conhecidos;
- usa a mesma sessão USER/client já existente no `pv_reply`;
- não cria Writer, Outbox, worker ou envio;
- persiste histórico + estado atual;
- distingue membro restrito de usuário realmente ausente;
- associa convite/grupo quando o update do Telegram fornecer informação suficiente.

Homologação real proposta:

```text
conta controlada recebe link
-> entra
-> conferir estado joined
-> sai
-> conferir estado left
-> entra novamente
-> conferir novo joined/join_count
```

Nenhuma resposta automática deve depender desse estado antes dessa prova.

## 6. PR documental de grupos

### PR #63 — Groups Operating Spec

**Estado:** manter aberta.

Ela continua útil porque especializa ADR-005/006 para:

- bootstrap de grupo;
- presença contínua;
- recuperação de post removido;
- grupo fechado/reabertura;
- arbitragem entre atendimento e repost;
- anti-travamento de `repost_pending`;
- painel operacional.

Não deve ser confundida com implementação já entregue.

## 7. PRs recicladas / encerradas como superseded

As seguintes PRs não devem ser promovidas na forma atual. Ideias úteis ficam preservadas neste plano/backlog e no histórico Git:

- **#40** — simulação MiniLearn: experimento superado e fora da prioridade atual sem IA;
- **#56** — módulo Entrada: substituído explicitamente pela #66;
- **#57** — Homenagem: branch antiga, conflitante; ideia preservada para implementação futura isolada;
- **#58** — Eventos: construída sobre a pilha antiga/#57; Live continua válida, mas deve ser encaixada depois no motor atual;
- **#59** — Remarketing: depende de `reminder_link`, removido pela #65;
- **#60** — Conversa Livre: superfície de IA/memória fora do escopo atual;
- **#61** — Contextos e Memória: read-model do MiniLearn fora do escopo atual e empilhado na linha antiga.

Fechar uma PR superseded não apaga branch nem histórico. Qualquer funcionalidade futura deve nascer novamente da produção atual, com diff exclusivo e CI nova.

## 8. Backlog preservado

### Duas Telas / Homenagem

Manter Duas Telas como engajamento. Captura de homenagem recebida do fã pode voltar depois, isoladamente, com:

- janela limitada após Duas Telas;
- foto/vídeo/GIF;
- sem guardar binário no banco;
- sem reutilização automática em marketing;
- resposta pronta editável;
- mesma Outbox/Writer/sessão.

### Live

Live permanece como mecanismo de proximidade. Futuro encaixe deve reutilizar os blocos `live_*` existentes e não criar motor paralelo.

### Respostas mínimas determinísticas

Após o motor linear e membership estarem homologados, criar catálogo pequeno, por exemplo:

- saudação;
- pediu link;
- não conseguiu entrar;
- agradecimento/confirmação;
- amenidades;
- despedida;
- videochamada, com resposta pronta de preço/duração/horário.

Cada intenção poderá ter várias versões. O estado por lead deve guardar a última versão/índice usado para evitar repetição consecutiva. Não depender de aleatoriedade como única proteção.

### Remarketing após saída

Somente depois de validar detecção de saída:

- cooldown explícito;
- uma única intenção ativa por lead;
- sem mencionar vigilância de saída;
- sem rajada após restart;
- sempre pelo Writer único.

## 9. Ordem operacional recomendada

Sem autorização de merge/deploy implícita neste documento.

Ordem técnica sugerida:

1. fechar/arquivar PRs superseded;
2. manter este plano como documento mestre atual;
3. revisar e promover #65 isoladamente quando autorizado;
4. revisar e promover #67 em shadow/passivo quando autorizado;
5. reancorar #66 na produção resultante;
6. CI completa da #66;
7. deploy da #66 com `PV_LINEAR_FLOW_ENABLED=OFF`;
8. simulação/teste controlado da linha;
9. ativação somente para homologação controlada;
10. depois construir respostas mínimas/variantes e integrar os fatos `joined/left` ao tom da conversa.

## 10. Protocolo de promoção

Para qualquer camada funcional:

```text
produção atual confirmada
-> diff exclusivo
-> CI verde
-> PR mergeável
-> merge isolado autorizado
-> deploy do mesmo serviço
-> conferir commit exato no Railway
-> deployment SUCCESS
-> logs
-> teste controlado
-> somente então ampliar escopo
```

Parar e pedir nova autorização se houver risco de:

- perda de dados;
- migração destrutiva;
- regeneração/rotação de credenciais ou StringSession;
- segunda sessão/Writer/Outbox;
- bloqueio relevante da conta Telegram;
- mudança irreversível.

## 11. Estado executivo

O projeto deixa de perseguir uma pilha grande de telas/IA e volta ao núcleo operacional:

**conversa linear + link + confirmação de entrada/saída + comunicação mínima determinística + engajamento por Duas Telas/Live.**

Esse é o baseline para as próximas decisões.
