# Kill switch por usuário no PV

## Objetivo

Parar imediatamente a automação privada para um único `user_id`, sem desligar o módulo PV inteiro. O bloqueio pode ser acionado pelo painel interno **ou pelo bloqueio nativo do próprio Telegram**.

## Entradas de bloqueio

- bloquear uma pessoa normalmente no Telegram — sincroniza automaticamente com o kill switch interno;
- `/parar_usuario` — abre no painel os 12 contatos PV mais recentes, com um botão `⛔` para cada pessoa;
- `/parar_usuario <ID ou @username>` — atalho direto quando a identidade é conhecida;
- `/usuarios_parados` — lista os usuários já suprimidos.

Todas as entradas convergem para o mesmo `suppress_pv_user`. Não existe uma segunda implementação de bloqueio.

## Bloqueio nativo do Telegram

A mesma sessão USER do GR-OBSERVER registra um handler `events.Raw(types.UpdatePeerBlocked)`. Quando o Telegram informa `blocked=True` na blocklist principal para um `PeerUser`, o sistema chama `suppress_pv_user` imediatamente.

Ao conectar o módulo PV, é feita uma única leitura limitada da blocklist principal com `contacts.getBlocked(offset=0, limit=100)`. Isso reconcilia usuários que já estavam bloqueados antes do deploy ou durante uma janela em que o listener não estava ativo. Não há varredura de diálogos e não é criada outra sessão Telegram.

Bloqueio apenas de stories (`blocked_my_stories_from=True`) não para o PV. Evento de desbloqueio (`blocked=False`) também **não reativa** a automação: reativação precisa ser explícita, para evitar que um desbloqueio acidental religue mensagens automáticas.

## Operação no celular

Ao enviar `/parar_usuario` sem parâmetro, o painel consulta `pv_reply_contacts` em ordem de `last_inbound_at DESC` e exclui quem já está em `pv_suppressed_users` com bloqueio ativo. O botão mostra nome e/ou `@username` quando disponíveis e termina com os quatro últimos dígitos do `user_id` para diferenciar homônimos. O callback `pvstop:<user_id>` chama o mesmo `suppress_pv_user` usado pelo comando direto.

## Garantias

- o bloqueio fica persistido em `pv_suppressed_users`;
- novas mensagens do usuário suprimido não avançam a jornada PV;
- ações PV pendentes para esse peer viram `succeeded` com resultado `admin_suppressed`, preservando o histórico;
- sessões de duas telas passam para `stopped`;
- campanhas de live são interrompidas para o usuário;
- o contato passa para `stage='stopped'`;
- nenhuma sessão, Outbox ou Writer adicional é criado;
- não há `DELETE`, `DROP` ou `TRUNCATE` de histórico operacional;
- a reconciliação da blocklist é uma leitura única e limitada; falha nessa leitura não derruba a sessão USER, pois o listener em tempo real continua ativo.

## Limite físico

Se uma RPC Telegram já estiver efetivamente em voo no exato instante em que o bloqueio acontece, essa única chamada pode terminar. O restante da fila é neutralizado e novas ações ficam bloqueadas.

## Registro de promoção

A PR #36 foi consolidada em `release/pr16-production` pelo merge `3d2d912db82a565b2119d9f81911b14786a28564`.

A PR #37 adicionou o seletor visual de contatos e foi consolidada pelo merge `5c3b938866fd91ee010efea5e1c767681d29f10c`.

A PR #38 adicionou a sincronização do bloqueio nativo do Telegram e foi consolidada pelo merge `510fca857a78cab34e7551a3a4f4881da4f3c15e`. Este commit registra a promoção e força o source deploy auditável da release.
