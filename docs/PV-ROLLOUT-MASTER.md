# Plano Mestre — Atendimento PV / Rollout Fino

- **Projeto:** `nt2digitize/gr-observer`
- **Documento vivo:** atualizar após cada promoção relevante
- **Data-base:** 2026-09-17
- **Produção:** `release/pr16-production` em `0d87a6d12113696a5dc2d7f2f20bb272c5d7c493`
- **Objetivo:** migrar o Atendimento PV para uma única conversa linear por lead, preservando a arquitetura de uma sessão USER, uma Outbox e um Writer.

## 1. Autoridade de fluxo

A conversa linear é o único chefe futuro da progressão do PV.

- quadros/fases são somente contexto visual;
- cada balão define conteúdo, espera antes do envio e se aguarda nova mensagem humana;
- Duas Telas, Live, membership e demais recursos são capacidades/fatos, não máquinas paralelas de jornada;
- opt-out, supressão, pacing, FloodWait e idempotência permanecem guardrails globais;
- `docs/PV-LINEAR-AUTHORITY.md` define o contrato completo de cutover e aposentadoria do legado.

## 2. Invariantes obrigatórios

- uma única sessão Telegram USER;
- um único `Observer.user_runtime`;
- uma única Outbox persistente;
- um único `SafeOutboxWriter`;
- um único `TrafficGovernor` global;
- `pv_reply` como uma única costela;
- uma lane lógica por `user_id`;
- nenhuma espera humana segura Writer ou fila global;
- toda mutação USER segue `módulo -> outbox_actions -> SafeOutboxWriter -> SafeTelegramEffects -> Telegram`;
- banco somente aditivo durante rollout;
- nenhum replay cego de efeito externo ambíguo;
- rollback por código/flag sem apagar histórico.

## 3. Decisão de produto vigente

Nesta etapa:

- sem IA, LLM, embeddings ou geração livre;
- respostas futuras serão determinísticas e aprovadas;
- objetivo principal do PV é conduzir ao grupo e manter comunicação mínima útil;
- registrar envio efetivo do link e detectar entrada/saída do grupo;
- Duas Telas preserva fotos pré-cadastradas e mecânica de escolha, mas perde autoridade própria de progressão;
- Live preserva evento/destino/consentimento, mas não cria jornada paralela;
- videochamada será tratada por resposta determinística aprovada, não por negociação automática.

## 4. PRs atuais

### #65 — remover `reminder_link` legado

Objetivo: eliminar o reenvio condicional standalone de link sem apagar histórico de banco.

Ordem prevista: primeira mudança funcional a ser promovida, após gate explícito de produção.

### #66 — conversa linear

Objetivo: motor único de conversa em linha.

Propriedades:

- `PV_LINEAR_FLOW_ENABLED` OFF por padrão;
- schema aditivo;
- ordem global de balões;
- texto/link/mídia do Telegram;
- espera antes do envio persistida;
- espera por resposta somente na lane da pessoa;
- enquanto ADR-004 estiver vigente, nenhuma espera anterior ao link inicial pode impedir a entrega do link;
- quando a flag estiver ON, progressões legadas ficam quarentenadas para não existir dois chefes;
- quando OFF, o legado continua disponível para rollback;
- opt-out continua guardrail global.

A flag não deve ser ligada em produção até Duas Telas/Live e demais capacidades necessárias à homologação terem sido reincorporadas sob autoridade da linha.

### #67 — membership passivo

Objetivo: observar entrada/saída/reentrada de leads conhecidos sem enviar nada.

Propriedades:

- mesmo cliente USER existente;
- nenhum Writer/Outbox/worker novo;
- persiste somente fatos mínimos: usuário, grupo, transição, razão, ordem, timestamps e se o convite observado correspondeu ao preview;
- não persiste o link privado bruto nem identidade do ator;
- eventos duplicados são idempotentes;
- evento Telegram atrasado pode ficar no histórico, mas não regressa o estado atual;
- desconhecidos não são persistidos.

Limite: #67 ainda não prova sozinho que um lead entrou especificamente pelo link enviado no PV quando o Telegram não informa o convite; a correlação `link_sent_at -> membership` permanece etapa separada.

### #62 — este plano mestre

Somente documentação. Deve permanecer alinhado ao código e aos ADRs vigentes.

### #63 — especificação operacional de grupos

Permanece separada do motor de PV e subordinada a ADR-005/006.

## 5. Pilha antiga encerrada

Foram encerradas como superadas, preservando branches e histórico:

- #40 MiniLearn experimental;
- #56 Entrada antiga;
- #57 Homenagem antiga;
- #58 Eventos da pilha antiga;
- #59 Remarketing antigo;
- #60 Conversa Livre;
- #61 Contextos/Memória.

Ideias úteis não autorizam promoção de código antigo. Qualquer reaproveitamento deve ser reconstruído sobre a arquitetura atual.

## 6. Próximas fases de bancada

1. deixar #65, #66 e #67 tecnicamente verdes e com diffs estritos;
2. registrar `link_sent_at` somente após sucesso real do efeito de envio, reutilizando fonte de verdade existente quando possível;
3. correlacionar membership com o destino conhecido sem persistir convite privado bruto;
4. transformar Duas Telas em capacidade chamada pela linha, preservando fotos/slots/proteção de repetição;
5. transformar Live em capacidade/evento chamado pela linha;
6. adicionar respostas determinísticas e variantes aprovadas sem criar segundo motor;
7. testar cutover virtual: backlog legado, restart, duplicidade, out-of-order, supressão, duas lanes e rollback por flag;
8. somente depois pedir autorização para merge/deploy.

## 7. Protocolo de promoção

Para cada mudança em produção:

1. produção atual confirmada;
2. diff exclusivo do escopo;
3. CI verde;
4. PR mergeável;
5. risco e rollback descritos;
6. CTA explícito do operador;
7. merge isolado;
8. CTA separado para deploy quando aplicável;
9. deployment SUCCESS;
10. logs e arquitetura conferidos;
11. teste manual controlado quando autorizado;
12. somente então próxima camada.

## 8. Proibições durante bancada

Sem novo CTA não executar:

- merge/deploy;
- ativação de flag em produção;
- alteração de variável/segredo;
- rotação de sessão/API/token;
- migração destrutiva;
- teste real que envie, entre, saia, adicione contato ou altere Telegram;
- segunda sessão/client/Outbox/Writer/fila/serviço.

## 9. Critério final de arquitetura

A migração PV estará concluída quando:

- a linha for a única autoridade de progressão;
- regras antigas não decidirem próximo passo;
- Duas Telas e Live forem capacidades chamadas pela linha;
- membership for somente fato;
- opt-out e guardrails continuarem independentes;
- legado substituído puder ser removido sem perda funcional;
- uma sessão USER, uma Outbox e um Writer permanecerem comprovados por CI.
