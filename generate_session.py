"""Login local para gerar uma StringSession exclusiva; nunca imprime segredos."""

import asyncio
from datetime import datetime
from getpass import getpass
import os
from pathlib import Path
import re
import sys


class SetupError(Exception):
    """Mensagem segura para exibir ao operador, sem valores de credenciais."""


def read_secret(prompt):
    """Windows usa caixa de texto com colagem nativa, fora de getpass/msvcrt."""
    if sys.platform != "win32":
        return getpass(prompt)
    try:
        import tkinter as tk
        from tkinter import simpledialog
    except ImportError:
        raise SetupError(
            "Este Python nao tem tkinter para abrir o campo de colagem. "
            "Informe essa mensagem; nao repita o comando antigo."
        ) from None
    root = None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        print("Preencha o campo na janela Radar GR (Ctrl+V funciona na caixa de texto).")
        value = simpledialog.askstring(
            "Radar GR - login local", prompt, show="*", parent=root
        )
        if value is None:
            raise EOFError
        return value
    except tk.TclError:
        raise SetupError(
            "Nao foi possivel abrir a janela Radar GR. "
            "Informe essa mensagem; nao repita o comando antigo."
        ) from None
    finally:
        if root is not None:
            root.destroy()


def clean_value(value):
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1].strip()
    return value


def validate_credentials(raw_id, raw_hash):
    api_id = clean_value(raw_id)
    api_hash = clean_value(raw_hash)
    if "${{" in api_id or "${{" in api_hash:
        raise SetupError(
            "Foi colada uma referencia do Railway. Copie os valores reais do servico de origem."
        )
    if not re.fullmatch(r"[0-9]+", api_id) or not 0 < int(api_id) < 2**31:
        raise SetupError(
            "API ID deve ser o numero do aplicativo Telegram, nao o ID do usuario ou telefone."
        )
    if ":" in api_hash:
        raise SetupError(
            "Esse valor parece um token de bot. API HASH vem de API development tools, nao do BotFather."
        )
    if not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise SetupError(
            f"O campo API HASH recebeu {len(api_hash)} caracteres depois de remover espacos das bordas. "
            "Esperado: 32 caracteres (0-9 e a-f). O conteudo nao foi exibido. "
            "O login nao foi iniciado."
        )
    return int(api_id), api_hash.lower()


def validate_phone(value):
    value = re.sub(r"[\s().-]", "", value)
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", value):
        raise SetupError(
            "Digite o telefone da conta no formato internacional: +, codigo do pais, DDD e numero."
        )
    return value


def telegram_runtime():
    # Import tardio permite verificar entrada e testar sem conectar ao Telegram.
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    return TelegramClient, StringSession, errors


def save_secret(path, value):
    """Cria somente um arquivo novo; nunca substitui uma sessao existente."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(value)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def archive_existing_session(path):
    """Move uma sessao anterior para backup sem ler ou imprimir seu conteudo."""
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = path.with_name(f"{path.stem}-anterior-{stamp}{path.suffix}")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.stem}-anterior-{stamp}-{suffix}{path.suffix}"
        )
        suffix += 1
    os.replace(path, candidate)
    return candidate


async def generate_session(api_id, api_hash, phone, destination):
    if destination.exists():
        raise SetupError(
            "Ja existe um arquivo de sessao no destino. "
            "Use o fluxo principal para arquiva-lo antes de gerar outra sessao."
        )
    TelegramClient, StringSession, errors = telegram_runtime()
    client = TelegramClient(
        StringSession(),
        api_id,
        api_hash,
        flood_sleep_threshold=0,
        request_retries=1,
        connection_retries=1,
    )
    try:
        await client.connect()
        sent = await client.send_code_request(phone)
        code = read_secret("Codigo recebido no Telegram (oculto): ").strip().replace(" ", "")
        if not code:
            raise SetupError("Codigo vazio. Nenhuma sessao foi salva.")
        try:
            await client.sign_in(
                phone=phone, code=code, phone_code_hash=sent.phone_code_hash
            )
        except errors.SessionPasswordNeededError:
            password = read_secret("Senha de duas etapas (oculta): ")
            await client.sign_in(password=password)
        me = await client.get_me()
        if me is None or getattr(me, "bot", False):
            raise SetupError(
                "Login nao confirmou uma conta de usuario. Nenhuma sessao foi salva."
            )
        session = client.session.save()
        if not session:
            raise SetupError(
                "O Telegram nao retornou uma sessao. Nenhum arquivo foi salvo."
            )
        save_secret(destination, session)
    except errors.ApiIdInvalidError:
        raise SetupError(
            "O Telegram rejeitou o par API ID/API HASH. Confira AMBOS no mesmo aplicativo em "
            "https://my.telegram.org/apps (API development tools). O formato local correto nao garante um par valido. "
            "Nao e o token do BotFather. Nenhuma sessao foi salva."
        ) from None
    except errors.FloodWaitError as exc:
        raise SetupError(
            f"Telegram pediu espera de {exc.seconds}s. O programa parou; nao repetiu o login."
        ) from None
    except errors.PhoneCodeInvalidError:
        raise SetupError(
            "O codigo de login foi rejeitado. Nenhuma sessao foi salva."
        ) from None
    except errors.PhoneCodeExpiredError:
        raise SetupError(
            "O codigo de login expirou. Nenhuma sessao foi salva."
        ) from None
    except errors.PasswordHashInvalidError:
        raise SetupError(
            "A senha de duas etapas foi rejeitada. Nenhuma sessao foi salva."
        ) from None
    except errors.RPCError as exc:
        raise SetupError(
            f"Telegram retornou {type(exc).__name__}. Nenhuma sessao foi salva."
        ) from None
    finally:
        try:
            await client.disconnect()
        except Exception:
            print("Nao foi possivel confirmar a desconexao; o processo sera encerrado.")


def main():
    print("Radar GR - gerador local de sessao exclusiva v3")
    print("API ID e API HASH devem pertencer ao MESMO aplicativo Telegram.")
    print(
        "No Windows, hash, telefone, codigo e senha sao preenchidos em janelas com campo oculto."
    )
    destination = Path(os.environ.get("USERPROFILE") or Path.home()) / "radar-gr-session.txt"
    archived = None
    try:
        raw_id = input("API ID (numero): ")
        raw_hash = read_secret("Cole o API HASH nesta caixa (32 caracteres, oculto): ")
        api_id, api_hash = validate_credentials(raw_id, raw_hash)
        print("Formato conferido. O par sera validado pelo Telegram no proximo passo.")
        phone = validate_phone(
            read_secret("Telefone da conta com +55 e DDD (oculto): ")
        )
        archived = archive_existing_session(destination)
        if archived:
            print(f"Sessao anterior arquivada em: {archived}")
        asyncio.run(generate_session(api_id, api_hash, phone, destination))
    except SetupError as exc:
        print(f"\n{exc}")
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nCancelado. Nenhuma nova sessao foi salva.")
        return 1
    except ImportError:
        print("Telethon ausente. Execute: python -m pip install telethon==1.44.0")
        return 1
    except OSError:
        print(
            "Falha de rede ou acesso ao arquivo. Confira a conexao e a pasta de usuario."
        )
        return 1
    except Exception as exc:
        # Nao imprimir repr/traceback: respostas RPC podem incluir dados de login.
        print(
            f"Falha: {type(exc).__name__}. Nao use esse resultado como uma sessao valida."
        )
        return 1
    print(f"\nNOVA sessao salva em: {destination}")
    print(
        "No Railway, substitua USER_SESSION_STRING do radar-gr-observer pelo conteudo DESTE arquivo e faca redeploy."
    )
    print(
        "Nao publique esse arquivo no GitHub, nao cole seu conteudo no chat e nao reutilize em outro processo."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
