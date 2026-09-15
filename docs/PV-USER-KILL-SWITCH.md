# Kill switch por usuário no PV

## Objetivo

Permitir ao administrador parar imediatamente a automação privada para um único `user_id`, sem desligar o módulo PV inteiro e sem depender do bloqueio manual do Telegram.

## Comandos

- `/parar_usuario <ID ou @username>`
- `/usuarios_parados`

## Garantias

- o bloqueio fica persistido em `pv_suppressed_users`;
- novas mensagens do usuário suprimido não avançam a jornada PV;
- ações PV pendentes para esse peer viram `succeeded` com resultado `admin_suppressed`, preservando o histórico;
- sessões de duas telas passam para `stopped`;
- campanhas de live são interrompidas para o usuário;
- o contato passa para `stage='stopped'`;
- nenhuma sessão, Outbox ou Writer adicional é criado;
- não há `DELETE`, `DROP` ou `TRUNCATE` de histórico operacional.

## Limite físico

Se uma RPC Telegram já estiver efetivamente em voo no exato instante em que o administrador aciona o kill switch, essa única chamada pode terminar. O restante da fila é neutralizado e novas ações ficam bloqueadas.

## Registro de promoção

A PR #36 foi consolidada em `release/pr16-production` pelo merge `3d2d912db82a565b2119d9f81911b14786a28564`. Este commit registra a promoção e força o source deploy auditável dessa release.
