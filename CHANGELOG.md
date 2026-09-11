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
