import asyncio
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import generate_session as login


class RPCError(Exception):
    pass


ERRORS = SimpleNamespace(RPCError=RPCError)
for name in ("ApiIdInvalidError", "FloodWaitError", "PhoneCodeInvalidError",
             "PhoneCodeExpiredError", "PasswordHashInvalidError", "SessionPasswordNeededError"):
    setattr(ERRORS, name, type(name, (RPCError,), {}))


class InputTests(unittest.TestCase):
    def test_whitespace_and_wrapping_quotes_are_removed(self):
        result = login.validate_credentials(' "123" ', " 'ABCDEF0123456789ABCDEF0123456789' ")
        self.assertEqual(result, (123, "abcdef0123456789abcdef0123456789"))

    def test_reference_token_and_masked_values_rejected(self):
        for value in ("${{service.TELEGRAM_API_HASH}}", "123:fake-token", "********", " ", "a" * 31):
            with self.subTest(value=value), self.assertRaises(login.SetupError):
                login.validate_credentials("123", value)

    def test_phone_id_is_not_an_api_id(self):
        with self.assertRaises(login.SetupError):
            login.validate_credentials("9999999999", "a" * 32)

    def test_phone_normalization_and_token_rejection(self):
        self.assertEqual(login.validate_phone("+1 (202) 555-0123"), "+12025550123")
        with self.assertRaises(login.SetupError):
            login.validate_phone("123:bot-token")

    def test_invalid_format_does_not_attempt_network(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.dict(login.os.environ, {"USERPROFILE": directory}), \
             patch("builtins.input", return_value="123"), \
             patch.object(login, "getpass", return_value="${{ref}}"), \
             patch.object(login, "telegram_runtime") as runtime, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(login.main(), 1)
            runtime.assert_not_called()


class LoginTests(unittest.IsolatedAsyncioTestCase):
    def fake_runtime(self):
        client = SimpleNamespace(
            connect=AsyncMock(), disconnect=AsyncMock(),
            send_code_request=AsyncMock(return_value=SimpleNamespace(phone_code_hash="opaque-test")),
            sign_in=AsyncMock(), get_me=AsyncMock(return_value=SimpleNamespace(bot=False)),
            session=SimpleNamespace(save=Mock(return_value="test-session-secret")),
        )
        return client, (Mock(return_value=client), Mock(return_value=object()), ERRORS)

    async def test_rejected_pair_disconnects_without_code_prompt_or_file(self):
        client, runtime = self.fake_runtime()
        client.send_code_request.side_effect = ERRORS.ApiIdInvalidError("sensitive-response")
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(login, "telegram_runtime", return_value=runtime), \
             patch.object(login, "getpass") as prompt:
            dest = Path(directory) / "session.txt"
            with self.assertRaises(login.SetupError) as result:
                await login.generate_session(123, "a" * 32, "+12025550123", dest)
            self.assertNotIn("sensitive-response", str(result.exception))
            self.assertFalse(dest.exists())
            prompt.assert_not_called()
            client.disconnect.assert_awaited_once()

    async def test_success_saves_only_session_not_credentials(self):
        client, runtime = self.fake_runtime()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(login, "telegram_runtime", return_value=runtime), \
             patch.object(login, "getpass", return_value="12345"), \
             contextlib.redirect_stdout(output):
            dest = Path(directory) / "session.txt"
            await login.generate_session(123, "a" * 32, "+12025550123", dest)
            self.assertEqual(dest.read_text(), "test-session-secret")
            self.assertNotIn("test-session-secret", output.getvalue())
            client.disconnect.assert_awaited_once()

    async def test_two_factor_flow(self):
        client, runtime = self.fake_runtime()
        client.sign_in.side_effect = [ERRORS.SessionPasswordNeededError(), None]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(login, "telegram_runtime", return_value=runtime), \
             patch.object(login, "getpass", side_effect=["12345", "test-password"]):
            await login.generate_session(123, "a" * 32, "+12025550123", Path(directory) / "session.txt")
            client.sign_in.assert_awaited_with(password="test-password")
            client.disconnect.assert_awaited_once()

    async def test_floodwait_stops_without_retrying(self):
        client, runtime = self.fake_runtime()
        error = ERRORS.FloodWaitError()
        error.seconds = 60
        client.send_code_request.side_effect = error
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(login, "telegram_runtime", return_value=runtime):
            with self.assertRaises(login.SetupError):
                await login.generate_session(123, "a" * 32, "+12025550123", Path(directory) / "session.txt")
            client.send_code_request.assert_awaited_once()
            client.disconnect.assert_awaited_once()

    async def test_existing_session_preserved_without_login(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(login, "telegram_runtime") as runtime:
            dest = Path(directory) / "session.txt"
            dest.write_text("existing-session")
            with self.assertRaises(login.SetupError):
                await login.generate_session(123, "a" * 32, "+12025550123", dest)
            self.assertEqual(dest.read_text(), "existing-session")
            runtime.assert_not_called()

    async def test_cancel_still_disconnects(self):
        client, runtime = self.fake_runtime()
        client.send_code_request.side_effect = asyncio.CancelledError()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(login, "telegram_runtime", return_value=runtime):
            with self.assertRaises(asyncio.CancelledError):
                await login.generate_session(123, "a" * 32, "+12025550123", Path(directory) / "session.txt")
            client.disconnect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
