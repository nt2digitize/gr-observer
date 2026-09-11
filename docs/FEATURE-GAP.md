# Inventário funcional e lacunas

Este documento separa o que o sistema realmente fazia antes da reorganização,
o que já ficou preparado no monólito e o que aparecia como plano mas não estava
implementado.

## O que existia de verdade

| Função | Situação encontrada | Situação nesta mudança |
|---|---|---|
| Painel administrativo | online | preservado e ampliado |
| Radar de grupos/canais | código completo, mas observação parada por sessão inválida | modularizado; ainda precisa de sessão nova para operar |
| Inventário/permissões/regras/bots/links | implementado no Radar | preservado |
| Origem provável de PV | implementada, janela de 7 dias | preservada como “provável” |
| Rascunho de resposta | grava no banco; não envia | preservado |
| Evidência por postagem manual | implementada | preservada |
| Teste BOTSON | rodando em serviço separado | integrado como costela, desligado por padrão |
| Saída + link de recuperação + reentrada | implementado no patch runtime do runner | incorporado ao motor da costela |
| Relatório BOTSON | somente em memória, expira | persistido; mantém expiração lógica de consulta |
| `telegram-inspector` | serviço sem deploy/offline | não reativado |

## O que estava planejado, mas não fazia

| Lacuna encontrada | Ainda é necessário? | Decisão recomendada |
|---|---|---|
| Análise estatística de rotina/horários | sim, útil | próxima costela somente leitura; baixo risco |
| Catálogo de donos e administradores | talvez | implementar só se houver uso operacional claro; dados podem ser incompletos |
| Aprovar/ignorar links encontrados | sim | adicionar workflow humano no painel antes de qualquer abertura/uso |
| Revisar e enviar rascunhos | talvez, depende da operação | primeiro revisão humana; envio sempre por Outbox |
| Postagem automática, mídia e agenda | não agora | adiar; aumenta risco de regra, FloodWait e Dual Write |
| Testar palavras proibidas em grupos reais | não de forma ativa | manter análise de regras publicadas; teste ativo só em ambiente autorizado |
| Inferir a função de cada bot | sim, como apoio | classificador por evidência, sem alegar acesso à configuração interna |
| Listar todos os membros | não | manter fora por privacidade, escala e FloodWait |
| Conversar automaticamente no PV | não agora | só após política, textos aprovados, opt-out e Outbox |
| Determinar origem exata do PV | tecnicamente não garantível | manter “origem provável” e nível de confiança |
| Crawler genérico permanente (`main.py`) | não como serviço separado | reaproveitar regras de navegação em testes on-demand; não abrir segunda sessão |
| Serviço offline `telegram-inspector` | provavelmente não | remover somente depois do cutover e confirmação humana |
| Fluxo pagamento → VIP (LivePix/Telegram) | necessário no projeto da Secretária, não neste Radar | aplicar Outbox/idempotência no repositório dono desse domínio; não misturar bancos aqui |

## O que falta para ativar, por depender de humano

1. Gerar uma nova `USER_SESSION_STRING`; a atual já foi invalidada pelo
   Telegram e nenhum código consegue recuperá-la.
2. Definir/confirmar `BOTSON_PREVIEW_ALLOWLIST` e o controlador (ID ou código de
   pareamento).
3. Escolher a janela de cutover.
4. Desligar o runner standalone antes de colocar a sessão nova no monólito.
5. Homologar uma Secretaria por vez e conferir o estado final de acesso.
6. Só depois decidir se os serviços antigos podem ser arquivados/removidos.

Nenhuma dessas decisões foi simulada ou tomada automaticamente.
