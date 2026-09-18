# Correção — membership da prévia antes do primeiro PV

## Problema
O tracker descartava eventos de entrada/saída de usuários ainda não conhecidos em `contact_ledger` ou `pv_reply_contacts`. Se a pessoa entrasse na prévia antes de falar no PV, o contexto posterior podia cair em `before_join` mesmo ela estando no grupo.

## Correção
Eventos do grupo de prévias passam a ser persistidos mesmo antes do primeiro PV, desde que o chat seja confirmado como a prévia configurada. Usuários desconhecidos em outros grupos continuam ignorados.

## Invariantes preservados
- uma única sessão Telegram USER;
- nenhuma nova Outbox, Writer, worker ou cliente;
- tracker continua passivo, sem envio Telegram;
- nenhum segredo, variável ou schema destrutivo alterado;
- motor linear continua controlado por `PV_LINEAR_FLOW_ENABLED`.

## Limite conhecido
Usuários que já estavam dentro da prévia antes desta correção precisam de uma nova transição de membership (sair/entrar) para o tracker reconstruir o estado via evento. Não há varredura de participantes nem chamada adicional ao Telegram nesta correção, evitando carga/flood desnecessário.
