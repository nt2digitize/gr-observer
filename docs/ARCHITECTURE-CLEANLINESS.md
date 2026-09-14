# Contrato de limpeza arquitetural

Este arquivo descreve somente o estado final exigido pela produção. Ele não é um registro de incidente nem um roteiro de rollback.

## Invariantes

- uma única sessão Telegram USER, criada no `Observer.user_runtime`;
- um único Outbox/Writer ativo para mutações da sessão USER;
- `application.py` é o único dono do ciclo de vida da sessão;
- FloodWait é deferido de forma persistente, sem `sleep` longo dentro do Writer;
- referência de peer é persistida e resolvida por tipos explícitos (`DurablePeerStore` / `DurableTelegramClient`);
- Radar de produção é passivo e dirigido por updates; não inicia crawler periódico;
- PV serializa transições do mesmo contato e invalida continuações que perderam o contexto;
- editor de PV não apaga conteúdo por navegação, cancelamento ou entrada vazia;
- nenhum componente troca métodos de objetos existentes em runtime.

## Evidência automatizada

`test_architecture_cleanliness.py` bloqueia no CI:

- arquivos temporários de deploy/rollback no tree ativo;
- overlays de hotfix obsoletos;
- `MethodType` e atribuições de métodos em runtime;
- mais de um `user_runtime`;
- mais de uma construção da sessão USER ou Writer dentro do runtime;
- DELETE/TRUNCATE/DROP nas políticas de Governor e peer rehydration;
- política de Radar que inicie scan loop periódico.

A proteção é estrutural: reintroduzir um desses padrões deve quebrar o CI antes de produção.
