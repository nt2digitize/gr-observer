# Ativação segura e rollback

O código e as migrações são automáticos; a autenticação da conta e a troca de
serviço exigem ação humana porque envolvem credencial e risco de invalidar outra
sessão.

## Pré-condições

- testes offline aprovados;
- branch de homologação revisada;
- todas as costelas desligadas antes da troca de credencial;
- nova StringSession gerada **depois** do último `AuthKeyDuplicatedError` e nunca
  usada em outro processo;
- nenhum serviço standalone, Termux, notebook ou processo local usando a mesma
  StringSession;
- convite da prévia cadastrado em `PV_PREVIEW_LINK` somente no Railway;
- `PV_REPLY_AUTO_ENABLE=true` somente quando o primeiro acionamento tiver sido
  autorizado; manter `false` nos demais ambientes;
- allowlist BOTSON revisada;
- controlador definido;
- backup lógico do PostgreSQL ou snapshot disponível.

## Regra de ouro da sessão

`AuthKeyDuplicatedError` invalida a chave afetada. Reimplantar, reiniciar ou
recolar o mesmo conteúdo de `radar-gr-session.txt` não corrige essa chave.

A trava consultiva PostgreSQL protege apenas o handoff entre processos que
compartilham o mesmo banco. Ela não autoriza usar a StringSession em outro
serviço, computador ou processo.

O gerador local `generate_session.py` arquiva automaticamente um
`radar-gr-session.txt` anterior e só então cria um novo. O conteúdo nunca deve
ser colado em chat, issue, log ou GitHub.

## Ordem de rotação quando a sessão está inválida

1. Manter Radar, Atendimento PV, Atendimento de Grupos e BOTSON desligados.
2. Confirmar que não existe runner standalone ou processo local usando a sessão.
3. No computador que possui o repositório, entrar na pasta `gr-observer`.
4. Executar `python generate_session.py`.
5. Concluir API ID, API HASH, telefone, código e 2FA, se solicitada.
6. Confirmar a mensagem `NOVA sessao salva em:`. O arquivo anterior, se existia,
   terá sido renomeado para `radar-gr-session-anterior-<data-hora>.txt`.
7. Copiar **somente** o conteúdo do novo `radar-gr-session.txt` para
   `USER_SESSION_STRING` do serviço `radar-gr-observer`.
8. Fazer um único redeploy.
9. Confirmar `Painel: online` e `Sessão única: offline` enquanto todas as
   costelas continuarem desligadas. Isso é esperado: não há conexão da conta sem
   uma função ligada.
10. Enviar `ligar radar` e conferir `Sessão única: online`, Radar `LIGADO` e
    ausência de `AuthKeyDuplicatedError`.
11. Só depois validar as outras costelas uma a uma.

Se o passo 10 retornar `AuthKeyDuplicatedError`, não repetir o mesmo arquivo.
Primeiro localizar e encerrar o segundo processo; depois gerar **outra** sessão.

## Ordem de ativação funcional

1. Implantar o monólito com as quatro costelas desligadas.
2. Confirmar `Painel: online` e executar `status`.
3. Confirmar que qualquer serviço antigo da mesma conta está parado.
4. Cadastrar uma nova `USER_SESSION_STRING` exclusiva somente no monólito.
5. Enviar `ligar radar`; validar inventário e ausência de
   `AuthKeyDuplicatedError`.
6. Enviar `ver mensagens pv` e conferir os textos/intervalos.
7. Se a ativação inicial automática não foi autorizada, enviar
   `ligar atendimento`. Testar com uma conta não-bot: primeiro PV, resposta e
   recebimento do link. Para os ciclos longos, conferir `next_followup_at` no
   banco em vez de aguardar em produção.
8. Enviar `ligar atendimento grupos`. A allowlist pode estar vazia quando a
   intenção é autorizar grupos por postagem manual; nesse caso, publicar
   manualmente em um grupo e conferir o cadastro do modelo.
9. Enviar `ligar botson`; parear se necessário.
10. Rodar `testar botson` com uma allowlist de uma Secretaria.
11. Conferir acesso final, Outbox e efeitos em `review`.
12. Ampliar allowlists apenas depois da homologação.

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

SELECT chat_id, message_id, user_id, status, sent_at
FROM group_reply_events ORDER BY created_at DESC LIMIT 30;
```

Uma linha `review` deve ser reconciliada; não altere para `pending` sem conferir
se o Telegram já realizou o efeito.

## Rollback

1. Enviar `desligar atendimento`, `desligar atendimento grupos`,
   `desligar botson` e `desligar radar`.
2. Aguardar `Sessão única: offline`.
3. Reimplantar o commit de rollback definido para a release.
4. Manter as tabelas novas; elas são aditivas.
5. Se for indispensável voltar a um runner separado, gerar/usar uma sessão
   exclusiva dele, nunca a mesma do monólito.
