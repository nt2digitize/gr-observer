# Correção — membership da prévia antes do primeiro PV

## Problema
O tracker descartava eventos de entrada/saída de usuários ainda não conhecidos em `contact_ledger` ou `pv_reply_contacts`. Se a pessoa entrasse na prévia antes de falar no PV, o contexto posterior podia cair em `before_join` mesmo ela estando no grupo.

## Correção
- Eventos do grupo de prévias passam a ser persistidos mesmo antes do primeiro PV, desde que o chat seja confirmado como a prévia configurada.
- Usuários desconhecidos em outros grupos continuam ignorados.
- Se o contexto ainda estiver ausente no primeiro envio linear, o mesmo cliente USER já conectado faz uma única consulta somente-leitura ao membership atual daquele lead na prévia e reconcilia o estado antes de selecionar o contexto.
- A reconciliação é tentada no máximo uma vez por peer por conexão quando o estado ainda é `before_join`, evitando chamadas repetidas e carga desnecessária.

## Invariantes preservados
- uma única sessão Telegram USER;
- nenhuma nova Outbox, Writer, worker ou cliente;
- nenhum envio Telegram adicional para reconciliar membership;
- nenhum segredo, variável ou schema destrutivo alterado;
- motor linear continua controlado por `PV_LINEAR_FLOW_ENABLED`;
- eventos de membership continuam passivos e isolados do avanço da conversa.

## Falha segura
Se a consulta atual ao Telegram não puder ser resolvida, a conversa não quebra: o erro é isolado, o contexto permanece no fallback existente e não há repetição da consulta em cada balão durante a mesma conexão.
