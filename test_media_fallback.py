import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telethon import functions, types

from gr_observer.outbox import (
    DefinitiveExternalEffectError,
    TelegramEffects,
    stable_random_id,
)
from gr_observer.panel import ControlPanel


class FakeUserClient:
    def __init__(self, source_message=None):
        self.source_message = source_message
        self.requests = []
        self.upload_file = AsyncMock(
            return_value=types.InputFile(
                id=1,
                parts=1,
                name="photo.jpg",
                md5_checksum="",
            )
        )

    async def get_messages(self, peer, ids):
        return self.source_message

    async def get_input_entity(self, peer):
        return types.InputPeerUser(user_id=int(peer), access_hash=1)

    async def __call__(self, request):
        self.requests.append(request)
        return NS(id=99)


class FakePanelClient:
    def __init__(self, source_message=None, media_bytes=b"panel-photo"):
        self.source_message = source_message
        self.get_messages_calls = []
        self.download_media = AsyncMock(return_value=media_bytes)
        self.send_file = AsyncMock()

    async def get_messages(self, peer, ids):
        self.get_messages_calls.append((peer, ids))
        return self.source_message


class MediaFallbackTests(unittest.IsolatedAsyncioTestCase):
    def storage(self):
        return NS(
            begin_effect=AsyncMock(return_value=("execute", None)),
            finish_effect=AsyncMock(),
            review_effect=AsyncMock(),
        )

    async def test_source_available_via_user_client(self):
        storage = self.storage()
        source_media = NS()
        user = FakeUserClient(NS(media=source_media))
        panel = FakePanelClient(NS(media=NS()))
        input_media = NS(spoiler=False)
        effects = TelegramEffects(storage, user, 1, panel_client=panel)

        with patch("gr_observer.outbox.utils.get_input_media", return_value=input_media) as build:
            result = await effects.send_catalogued_media(
                123, 77, 456, "photo:user", spoiler=True, ttl_seconds=30
            )

        self.assertEqual(result["source_client"], "user")
        self.assertEqual(result["ttl_seconds"], 30)
        self.assertTrue(result["spoiler"])
        self.assertEqual(result["random_id"], stable_random_id("photo:user"))
        build.assert_called_once_with(source_media, ttl=30)
        self.assertTrue(input_media.spoiler)
        self.assertFalse(panel.get_messages_calls)
        user.upload_file.assert_not_awaited()

    async def test_fallback_via_panel_client_downloads_and_uploads(self):
        storage = self.storage()
        user = FakeUserClient(None)
        panel_message = NS(media=NS())
        panel = FakePanelClient(panel_message, b"legacy-photo")
        effects = TelegramEffects(storage, user, 2, panel_client=panel)

        result = await effects.send_catalogued_media(
            123, 77, 456, "photo:panel", spoiler=True, ttl_seconds=30
        )

        self.assertEqual(result["source_client"], "panel")
        panel.download_media.assert_awaited_once_with(panel_message, file=bytes)
        user.upload_file.assert_awaited_once_with(
            b"legacy-photo", file_name="photo.jpg"
        )
        request = user.requests[-1]
        self.assertIsInstance(request, functions.messages.SendMediaRequest)
        self.assertIsInstance(request.media, types.InputMediaUploadedPhoto)
        self.assertTrue(request.media.spoiler)
        self.assertEqual(request.media.ttl_seconds, 30)
        self.assertEqual(request.random_id, stable_random_id("photo:panel"))

    async def test_missing_in_both_clients_is_definitive(self):
        storage = self.storage()
        user = FakeUserClient(None)
        panel = FakePanelClient(None)
        effects = TelegramEffects(storage, user, 3, panel_client=panel)

        with self.assertRaises(DefinitiveExternalEffectError):
            await effects.send_catalogued_media(
                123, 77, 456, "photo:missing", spoiler=True, ttl_seconds=30
            )

        storage.finish_effect.assert_awaited_once()
        storage.review_effect.assert_not_awaited()
        self.assertFalse(user.requests)


class PanelMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_row_is_healthy_and_has_view_and_replace_buttons(self):
        media_message = NS(media=NS())
        panel_client = FakePanelClient(media_message)
        user_client = FakeUserClient(None)
        pool = NS(
            fetch=AsyncMock(
                return_value=[
                    {
                        "slot": "peitos",
                        "source_peer": 123,
                        "source_message_id": 77,
                        "updated_at": None,
                    }
                ]
            )
        )
        app = NS(
            panel=panel_client,
            user=user_client,
            pool=pool,
            admin_id=123,
        )
        panel = ControlPanel(app)
        event = NS(respond=AsyncMock())

        await panel.show_two_screens_media(event)

        response = event.respond.await_args
        text = response.args[0]
        buttons = response.kwargs["buttons"]
        button_texts = [button.text for row in buttons for button in row]
        self.assertIn("• peitos: ✅ saudável", text)
        self.assertTrue(any("👁 Ver foto — peitos" == text for text in button_texts))
        self.assertTrue(any("♻️ Trocar foto — peitos" == text for text in button_texts))
        self.assertEqual(
            panel_client.get_messages_calls,
            [(123, 77)],
        )

    async def test_view_sends_inline_photo_not_document(self):
        media_bytes = b"jpeg-preview"
        media_message = NS(media=NS())
        panel_client = FakePanelClient(media_message, media_bytes)
        user_client = FakeUserClient(None)
        pool = NS(
            fetchrow=AsyncMock(
                return_value={
                    "slot": "peitos",
                    "source_peer": 123,
                    "source_message_id": 77,
                    "updated_at": None,
                }
            )
        )
        app = NS(
            panel=panel_client,
            user=user_client,
            pool=pool,
            admin_id=123,
            is_admin=lambda _event: True,
        )
        panel = ControlPanel(app)
        event = NS(
            data=b"pv:two_screens:view:peitos",
            answer=AsyncMock(),
        )

        await panel.on_callback(event)

        panel_client.download_media.assert_awaited_once_with(media_message, file=bytes)
        panel_client.send_file.assert_awaited_once()
        send = panel_client.send_file.await_args
        preview_file = send.args[1]
        self.assertEqual(preview_file.name, "peitos.jpg")
        self.assertEqual(preview_file.getvalue(), media_bytes)
        self.assertIs(send.kwargs["force_document"], False)
        self.assertEqual(send.kwargs["caption"], "👁 peitos")

    async def test_invalid_legacy_reference_is_explicit(self):
        panel_client = FakePanelClient(None)
        user_client = FakeUserClient(None)
        pool = NS(
            fetch=AsyncMock(
                return_value=[
                    {
                        "slot": "cu",
                        "source_peer": 123,
                        "source_message_id": 88,
                        "updated_at": None,
                    }
                ]
            )
        )
        app = NS(panel=panel_client, user=user_client, pool=pool, admin_id=123)
        panel = ControlPanel(app)
        event = NS(respond=AsyncMock())

        await panel.show_two_screens_media(event)

        text = event.respond.await_args.args[0]
        self.assertIn("• cu: ⚠️ referência inválida — recadastre", text)
        self.assertIn("• peitos: ⚪ falta cadastrar", text)
        self.assertIn("• buceta: ⚪ falta cadastrar", text)


if __name__ == "__main__":
    unittest.main()
