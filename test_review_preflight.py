import inspect
import unittest

from gr_observer.outbox import TelegramEffects


class ReviewPreflightTests(unittest.TestCase):
    def test_peer_resolution_happens_before_effect_journal(self):
        source = inspect.getsource(TelegramEffects._send_text)
        resolve_at = source.index("input_peer = await client.get_input_entity(peer)")
        perform_at = source.index("return await self.perform(")
        self.assertLess(resolve_at, perform_at)
        send_block = source[source.index("async def send():"):perform_at]
        self.assertNotIn("get_input_entity(peer)", send_block)


if __name__ == "__main__":
    unittest.main()
