# Ativação segura e rollback

O código e as migrações são automáticos; a autenticação da conta e a troca de
serviço exigem ação humana porque envolvem credencial e risco de invalidar outra
sessão.

## Pré-condições

- testes offline aprovados;
- branch de homologação revisada;
- nova StringSession gerada e nunca usada em outro processo;
- convite da prévia cadastrado em `PV_PREVIEW_LINK` somente no Railway;
- `PV_REPLY_AUTO_ENABLE=true` somente quando o primeiro acionamento tiver sido
  autorizado; manter `false` nos demais ambientes;
- allowlist BOTSON revisada;
- controlador definido;
- backup lógico do PostgreSQL ou snapshot disponível.

## Ordem de ativação

1. Implantar o monólito com Radar, Atendimento PV e BOTSON desligados.
2. Confirmar `Painel: online` e executar `status`.
3. Parar o serviço `telegram-inspector-runner` antigo.
4. Confirmar que ele não mantém conexão ativa.
5. Cadastrar a nova `USER_SESSION_STRING` somente no monólito.
6. Enviar `ligar radar`; validar inventário e ausência de
   `AuthKeyDuplicatedError`.
7. Enviar `ver mensagens pv` e conferir os textos/intervalos.
8. Se a ativação inicial automática não foi autorizada, enviar
   `ligar atendimento`. Testar com uma conta não-bot: primeiro PV, resposta e
   recebimento do link. Para os ciclos longos, conferir `next_followup_at` no
   banco em vez de aguardar em produção.
9. Enviar `ligar botson`; parear se necessário.
10. Rodar `testar botson` com uma allowlist de uma Secretaria.
11. Conferir acesso final, Outbox e efeitos em `review`.
12. Ampliar a allowlist apenas depois da homologação.

Não existe etapa em que dois serviços recebem a mesma StringSession.

## Consultas operacionais

```sql
SELECT module_id, enabled, reason, updated_at
FROM module_control ORDER BY module_id;

SELECT id, action_key, status, attempts, last_error, updated_at
FROM outbox_actions ORDER BY id DESC LIMIT 30;

SELECT effect_key, effect_type, status, last_error, updated_at
FROM telegram_effects ORDER BY updated_at DESC LIMIT 30;

SELECT run_id, status, error, created_at, finished_at
FROM module_runs ORDER BY created_at DESC LIMIT 20;

SELECT user_id, stage, followup_cycle, weekly_cycle, next_followup_at
FROM pv_reply_contacts ORDER BY updated_at DESC LIMIT 30;
```

Uma linha `review` deve ser reconciliada; não altere para `pending` sem conferir
se o Telegram já realizou o efeito.

## Rollback

1. Enviar `desligar atendimento`, `desligar botson` e `desligar radar`.
2. Aguardar `Sessão única: offline`.
3. Reimplantar `65ce874`.
4. Manter as tabelas novas; elas são aditivas.
5. Se for indispensável voltar ao runner separado, gerar/usar uma sessão
   exclusiva dele, nunca a mesma do monólito.
