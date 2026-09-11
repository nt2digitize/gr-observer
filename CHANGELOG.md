# Changelog

## Não lançado — monólito modular

### Adicionado

- catálogo ordenado de módulos, comandos, detalhes e textos;
- registro independente das costelas Radar e Testar BOTSON;
- sessão de usuário única com trava de handoff entre deploys;
- Transactional Inbox/Outbox, Writer único e diário de efeitos;
- mensagens com `random_id` determinístico e reconciliação de entrada/saída;
- persistência de execuções e relatórios BOTSON;
- painel `/funcoes` e comandos em texto natural;
- documentação de arquitetura, decisão, lacunas, ativação e rollback;
- testes de isolamento, catálogo, segurança, idempotência e sessão.

### Alterado

- `app.py` virou entrada fina, mantendo o comando de produção;
- o Radar foi movido para módulo sem alterar seu ato final passivo;
- o fluxo BOTSON do runner foi adaptado para a sessão/Writer compartilhados;
- pareamento novo é persistido no PostgreSQL, com migração do marcador antigo.

### Segurança

- `AuthKeyDuplicatedError` informa que uma sessão nova é obrigatória;
- callbacks de resultado incerto não são repetidos automaticamente;
- a função BOTSON nasce desligada e exige allowlist mais controlador/pareamento;
- operações de pagamento, compra, assinatura e moderação continuam bloqueadas.

## Não lançado — Atendimento PV

### Adicionado

- costela `pv_reply`, desligada por padrão e controlável por
  `ligar/desligar atendimento`;
- textos ordenados em `CAMPAIGNS` e prévia administrativa por
  `ver mensagens pv`;
- duas mensagens iniciais com atraso: cumprimento e link somente após a
  resposta seguinte;
- intervalos determinísticos de aproximadamente 1, 2, 3, 4, 5, 6 e 7 dias;
- após atingir sete dias, pergunta semanal sem link; resposta positiva encerra
  em silêncio e negativa recebe o link;
- opt-out, filtro de contas marcadas como bot e estado persistente por contato;
- agendamento durável por `outbox_actions.available_at` e estado em
  `pv_reply_contacts`.

### Privacidade e operação

- conteúdo recebido no PV não é salvo pela função;
- convite privado fica em `PV_PREVIEW_LINK`, fora do repositório público;
- ativação inicial opcional por `PV_REPLY_AUTO_ENABLE`, sem sobrepor uma pausa
  posterior feita pelo administrador;
- credenciais, StringSession e convite privado nunca entram no repositório.
