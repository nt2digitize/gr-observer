# Kill switch por usuário no PV

## Objetivo

Permitir ao administrador parar imediatamente a automação privada para um único `user_id`, sem desligar o módulo PV inteiro e sem depender do bloqueio manual do Telegram.

## Comandos

- `/parar_usuario` — abre no painel os 12 contatos PV mais recentes, com um botão `⛔` para cada pessoa;
- `/parar_usuario <ID ou @username>` — atalho direto quando a identidade é conhecida;
- `/usuarios_parados` — lista os usuários já suprimidos.

## Operação no celular

Ao enviar `/parar_usuario` sem parâmetro, o painel consulta `pv_reply_contacts` em ordem de `last_inbound_at DESC` e exclui quem já está em `pv_suppressed_users` com bloqueio ativo. O botão mostra nome e/ou `@username` quando disponíveis e termina com os quatro últimos dígitos do `user_id` para diferenciar homônimos. O callback `pvstop:<user_id>` chama o mesmo `suppress_pv_user` usado pelo comando direto; não existe uma segunda implementação de bloqueio.

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

A PR #36 foi consolidada em `release/pr16-production` pelo merge `3d2d912db82a565b2119d9f81911b14786a28564`.

A PR #37 adicionou o seletor visual de contatos e foi consolidada pelo merge `5c3b938866fd91ee010efea5e1c767681d29f10c`. Este commit registra a promoção e força o source deploy auditável da release com o picker móvel.