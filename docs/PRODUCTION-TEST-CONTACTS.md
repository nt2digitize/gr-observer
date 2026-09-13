# Homologação — contatos, ADD e proteção de engajamento

Este pacote deve entrar em produção com todas as funções novas desligadas. A ativação é progressiva e reversível por variável de ambiente.

## Pré-condições

- PR sazonal/árbitro de grupos já integrado;
- CI desta branch verde;
- mesma sessão Telegram única do monólito;
- nenhuma instância standalone usando a StringSession;
- `/flood` sem janela ativa relevante antes do teste.

## Etapa 0 — deploy inerte

Manter:

- `PV_AUTO_SAVE_CONTACTS=false`
- `GROUP_ADD_CONTACT_FLOW=false`
- `GROUP_ENGAGEMENT_PROTECTION=false`

Validar que o serviço sobe, a sessão fica online e não aparecem ações novas de contato/ADD por simples tráfego.

## Etapa 1 — autosalvamento PV

Ativar somente:

`PV_AUTO_SAVE_CONTACTS=true`

Com uma conta de teste conhecida:

1. enviar uma mensagem no PV;
2. confirmar que o fluxo PV normal continua;
3. confirmar que o contato aparece salvo na conta operadora;
4. enviar outra mensagem e confirmar ausência de duplicação;
5. repetir com contato já salvo;
6. repetir com usuário cujo telefone não esteja visível.

Critérios de aprovação:

- nenhuma confirmação falsa;
- nenhuma mensagem extra por causa do salvamento;
- nenhuma duplicação de contato/efeito;
- sem FloodWait anormal.

## Etapa 2 — ADD em grupo autorizado

Ativar:

`GROUP_ADD_CONTACT_FLOW=true`

Somente em grupo já autorizado para o módulo.

1. conta de teste responde diretamente à nossa mensagem: `add, tô de ban`;
2. aguardar a janela estável de 25–35 s, além do pacing global quando aplicável;
3. confirmar que o contato foi salvo primeiro;
4. confirmar reply na mensagem original: `já add, chama lá`;
5. testar `@username me adiciona`;
6. testar `add` sem reply e sem menção: não deve disparar;
7. testar usuário já salvo: deve responder sem tentar duplicar o contato;
8. provocar/usar um usuário não resolvível: não deve enviar `já add`.

## Etapa 3 — proteção de engajamento

Para homologação curta, usar temporariamente:

- `GROUP_ENGAGEMENT_PROTECTION=true`
- `GROUP_ENGAGEMENT_PROTECTION_SECONDS=120`

1. responder diretamente à publicação corrente do loop;
2. deixar o próximo repost ocorrer;
3. confirmar que a publicação antiga protegida permanece;
4. interagir novamente antes de 120 s e confirmar renovação da janela;
5. confirmar que a limpeza antiga não apaga a mensagem renovada;
6. após expirar, confirmar que a mensagem antiga é removida somente se já não for a publicação corrente;
7. confirmar que resposta reativa comum do atendimento não vira alvo de limpeza.

Depois da homologação, restaurar:

`GROUP_ENGAGEMENT_PROTECTION_SECONDS=86400`

## Kill switch

Em caso de comportamento inesperado:

- `PV_AUTO_SAVE_CONTACTS=false`
- `GROUP_ADD_CONTACT_FLOW=false`
- `GROUP_ENGAGEMENT_PROTECTION=false`

Após o restart causado pela alteração de variáveis, ações pendentes de salvamento/ADD viram no-op. Limpezas já agendadas de mensagens previamente protegidas continuam permitidas para não deixar lixo permanente.

## Go / No-Go

**GO** somente se:

- CI verde;
- sessão única saudável;
- zero confirmação `já add` sem contato confirmado;
- nenhuma duplicidade após restart;
- proteção renovável testada;
- nenhuma deleção de mensagem que não pertença ao loop;
- FloodWait dentro do comportamento esperado;
- rollback por flags validado.

Qualquer violação acima é **NO-GO**.
