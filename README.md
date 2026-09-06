# GR Observer Cell

MVP de observação passiva para uma conta Telegram. Ele inventaria os grupos e
canais já acessíveis, registra permissões, regras publicadas, bots vistos,
links e a provável origem de uma pessoa que chama no privado.

## Limites deliberados

- não entra em links;
- não envia mensagens em grupos ou canais;
- não clica em botões;
- não lista todos os membros;
- não testa filtros ou palavras proibidas;
- não responde automaticamente no privado;
- uma sugestão de resposta vira rascunho, nunca envio.

Filtros secretos de bots moderadores não são visíveis pela API. O sistema só
marca regras publicadas e evidências observadas.

## Instalação

1. Crie um PostgreSQL e um bot privado de controle no BotFather.
2. Copie `.env.example` para `.env` e preencha as variáveis.
3. Use uma `USER_SESSION_STRING` exclusiva para este serviço.
4. Instale: `pip install -r requirements.txt`.
5. Execute: `python app.py`.
6. No privado do bot de controle, envie `/observador`.

No Railway, use `python app.py` como comando inicial e cadastre as mesmas
variáveis. Apenas o `ADMIN_USER_ID` consegue abrir o painel.

## Fluxo de origem do PV

Quando alguém responde ou menciona a conta em um grupo, a célula registra uma
interação por sete dias. Se a mesma pessoa chamar no privado nesse período, o
PV recebe internamente a origem provável e aparece em **Origens PV** no painel.
Nenhuma mensagem é enviada à pessoa.

## Regra obrigatória de linguagem

Toda mensagem destinada a membros, leads ou contatos deve parecer conversa
nativa de chat: frases curtas, abreviações naturais (`vc`, `q`, `pq`, `tb`,
`tô`, `blz`) e pequenas variações de ritmo. Não usar texto formal, perfeito ou
com cara de atendimento automático. Não exagerar nos erros nem repetir sempre
as mesmas abreviações. Textos técnicos do painel administrativo não seguem
essa regra.

Exemplo inadequado: `Vi que você veio pelo grupo. Vou confirmar o link correto para você.`

Exemplo adequado: `pera aí q vou ver o link certo p vc`

## Estado e implantação

O painel inicia sem a sessão do observador; /ligar informa quando ela falta.
Se faltar uma credencial do próprio painel, o processo termina e informa
somente os nomes das variáveis ausentes. Use uma única réplica.
Reinício automático está desativado: FloodWait exige revisão manual.
O bot aceita /start e /observador somente no privado do ADMIN_USER_ID.
As credenciais nunca devem ser commitadas. Cadastre-as em Variables no Railway.

Limitações do MVP: trechos candidatos a regras exigem leitura humana; mídia é
uma indicação geral, sem discriminar cada formato; prévias de links não provam
permissão de divulgação; bots vistos não revelam sua configuração interna.
Ainda não há análise estatística da rotina, catálogo de donos/admins,
aprovação/ignorar links nem envio de rascunhos. Origens são apenas prováveis.
O histórico lido é limitado a 20 mensagens por chat por varredura por padrão.
Interações, origens e rascunhos pessoais expiram em sete dias (limpeza na varredura).

Referência: https://docs.telethon.dev/en/stable/modules/custom.html

## Controle administrativo

No privado do bot, somente o ADMIN_USER_ID pode executar:

- `/ligar`: conecta a conta observadora e inicia a leitura.
- `/desligar`: cancela a leitura e desconecta a conta observadora; mantém o painel online.
- `/status`: informa o estado real e o motivo de uma pausa.
- `/start` ou `/observador`: abre o painel com botões Ligar/Desligar.

O estado fica na tabela observer_control do PostgreSQL e é restaurado após
reiniciar o serviço. A primeira instalação começa desligada. Sem
USER_SESSION_STRING o painel funciona, mas a observação não liga.
FloodWait pausa apenas a observação e salva a pausa; o painel segue acessível.
Nenhum comando permite postar, entrar em grupos ou enviar mensagens pela conta.

## Gerar uma sessao exclusiva no computador

Instale `telethon==1.44.0` e execute `python generate_session.py` em um terminal
interativo. O gerador usa uma StringSession vazia e pede API ID, API HASH,
telefone, codigo de login e, quando exigida, senha de duas etapas. O codigo,
telefone, hash e senha ficam ocultos no terminal; nao sao gravados em arquivos
de configuracao nem incluidos no historico de comandos.

No Windows, a versao 2 abre janelas de entrada com campos mascarados para hash,
telefone, codigo e senha. Isso permite colar com Ctrl+V em uma caixa de texto
sem depender do comportamento de `getpass` no console. Em outros sistemas,
mantem a entrada oculta no terminal. Uma falha de formato informa apenas a
quantidade de caracteres recebidos, nunca o conteudo. Se tkinter nao estiver
disponivel, o gerador informa esse problema sem voltar ao campo de console.

Use o ID e hash do MESMO aplicativo, obtidos em API development tools de
https://my.telegram.org/apps. Tambem pode copiar os valores reais do servico
de origem no Railway; expressoes `${{...}}` e tokens do BotFather nao servem.
O gerador verifica apenas o formato localmente. A validade do par depende da
resposta do Telegram. `ApiIdInvalidError` significa que o par foi rejeitado:
confira ambos os valores, sem repetir com as mesmas credenciais.

O arquivo `radar-gr-session.txt` so e criado apos autenticar uma conta de
usuario. Ele fica na pasta de usuario do Windows, fora do repositorio, e nao e
sobrescrito se ja existir. Copie seu conteudo diretamente para
`USER_SESSION_STRING` no Railway, faca deploy e use `/ligar`. O gerador nao
roda no Railway e nao precisa permanecer aberto. A sessao e sensivel;
`radar-gr-session*.txt` tambem foi incluido no `.gitignore` e `.dockerignore`.

Erros de autenticacao terminam com mensagem curta e a conexao e encerrada em
`finally`. Os testes locais simulam rede e autenticacao; nao validam credenciais
reais nem fazem login.

Referencia: https://docs.telethon.dev/en/stable/basic/signing-in.html
