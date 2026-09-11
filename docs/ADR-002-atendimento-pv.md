# ADR-002 — Atendimento PV com agenda durável

- **Status:** implementado localmente; ainda não ativado em produção
- **Data:** 2026-09-10
- **Escopo:** somente `nt2digitize/gr-observer`

## Contexto

O Radar apenas observava o privado e podia criar rascunhos. A nova necessidade
é responder pessoas que iniciam um PV, entregar o canal de prévias somente após
uma segunda mensagem e manter contatos amistosos com intervalos crescentes.
Como há banco, relógio e Telegram na mesma ação lógica, um `sleep` em memória
não preservaria a agenda durante reinícios e repetiria mensagens em alguns
cenários de queda.

## Decisão

Adicionar a costela independente `pv_reply`, desligada por padrão. Ela usa a
mesma sessão de usuário e o Writer do monólito, sem criar outro
`TelegramClient`.

Textos aprovados no catálogo:

1. `Oi 😊 Tudo bem? Quer ver a minha esposa puta?`
2. `Se quiser ver mais, entre no canal de prévias 😊` + convite privado.
3. Primeira fase: `Gostou? Já entrou no canal de prévias? 😊` + convite.
4. Fase semanal: `Oi 😊 Já entrou no canal de prévias? Gostou?`, sem convite.

O convite não é hardcoded porque o repositório é público. Ele deve existir
somente em `PV_PREVIEW_LINK` no Railway.

## Regras da conversa

- somente mensagem privada recebida inicia o fluxo;
- contas marcadas pelo Telegram como bot, apagadas, de suporte e `777000` são
  ignoradas;
- após o primeiro PV, a pergunta inicial aguarda 60 segundos;
- qualquer resposta seguinte, exceto opt-out, libera o convite após 60
  segundos;
- a primeira fase inclui o convite e usa intervalos estáveis com jitter:
  23–25 h, 47–49 h, 71–73 h, 95–97 h, 119–121 h, 143–145 h e 167–169 h;
- o teto de sete dias é do intervalo entre contatos; os sete ciclos somam
  aproximadamente 28 dias antes da fase semanal;
- depois, a pergunta sem link ocorre a cada 168 h;
- resposta positiva conclui o fluxo sem enviar confirmação;
- resposta negativa clara recebe novamente o convite;
- texto ambíguo não gera resposta extra;
- `parar`, `não quero` e equivalentes encerram o fluxo.

“Pessoa real” não é verificável pelo Telegram. O contrato técnico é apenas
“conta não marcada como bot” com as exclusões acima.

## Persistência e Dual Write

`pv_reply_contacts` guarda contato, etapa, ciclos e próxima execução. O conteúdo
do PV não é salvo. `inbox_events` deduplica cada mensagem recebida. A intenção
de responder é criada na mesma transação e `outbox_actions.available_at` guarda
o horário futuro.

Cada envio passa por `telegram_effects` com chave e `random_id` determinísticos.
Após um envio, a mudança de etapa e a criação da próxima ação são atômicas no
PostgreSQL. Se o processo cair na janela entre Telegram e essa transação, o
efeito fica rastreável em `review`; não há repetição cega.

Desligar a costela pausa ações pendentes. Elas não são consumidas enquanto
`module_control.enabled` estiver falso.

## Operação

- `ligar atendimento` / `/ligar_atendimento`;
- `desligar atendimento` / `/desligar_atendimento`;
- `ver mensagens pv` / `/mensagens_pv`.

A ativação real continua bloqueada enquanto a StringSession estiver inválida e
até `PV_PREVIEW_LINK` ser cadastrado com segurança.
