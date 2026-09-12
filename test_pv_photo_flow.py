import inspect
import unittest

from gr_observer.modules.pv_reply import PvReplyModule, classify_two_screens_choice
from gr_observer.outbox import TelegramEffects
from gr_observer.pv_photo_flow import (
    EXTRA_PHOTO_DELAY_RANGE_SECONDS,
    PHOTO_SLOTS,
    caption_for_slot,
    choose_unsent_slot,
    stable_delay_seconds,
)


class PvPhotoFlowTests(unittest.TestCase):
    def test_three_slots_are_preserved(self):
        self.assertEqual(PHOTO_SLOTS, ("peitos", "buceta", "cu"))

    def test_preference_aliases(self):
        self.assertEqual(classify_two_screens_choice("peitão"), "peitos")
        self.assertEqual(classify_two_screens_choice("manda buceta"), "buceta")
        self.assertEqual(classify_two_screens_choice("quero cuzinho"), "cu")
        self.assertEqual(classify_two_screens_choice("bunda"), "cu")
        self.assertIsNone(classify_two_screens_choice("surpreende aí"))

    def test_unknown_choice_falls_back_only_to_unsent_slot(self):
        slot = choose_unsent_slot(10, 20, ("buceta", "cu"))
        self.assertIn(slot, {"buceta", "cu"})
        self.assertNotEqual(slot, "peitos")

    def test_extra_photo_delay_is_25_to_35_minutes(self):
        low, high = EXTRA_PHOTO_DELAY_RANGE_SECONDS
        self.assertEqual(low, 25 * 60)
        self.assertEqual(high, 35 * 60)
        value = stable_delay_seconds("test", low, high)
        self.assertGreaterEqual(value, low)
        self.assertLessEqual(value, high)

    def test_category_caption_matches_slot(self):
        self.assertEqual(caption_for_slot("peitos"), "Goza aí nesse peitão.")
        self.assertEqual(caption_for_slot("buceta"), "Goza aí nessa buceta.")
        self.assertEqual(caption_for_slot("cu"), "Goza aí nesse cuzinho.")

    def test_prompt_goes_directly_to_preference_without_yes_no_gate(self):
        source = inspect.getsource(PvReplyModule.action_send_two_screens_prompt)
        self.assertIn('DIALOGS["pv.two_screens.preference"]', source)
        self.assertIn('"awaiting_choice"', source)
        self.assertNotIn('"awaiting_optin"', source)

    def test_photo_action_sends_caption_and_reopens_until_final(self):
        source = inspect.getsource(PvReplyModule.action_send_two_screens_photo)
        self.assertIn("caption_for_slot", source)
        self.assertIn("mark_photo_sent", source)
        self.assertIn("final=final", source)

    def test_production_photo_uses_writer_spoiler_and_30_second_ttl(self):
        source = inspect.getsource(PvReplyModule.action_send_two_screens_photo)
        self.assertIn("send_catalogued_media", source)
        self.assertIn("spoiler=True", source)
        self.assertIn("ttl_seconds=TWO_SCREENS_PHOTO_TTL_SECONDS", source)

    def test_writer_media_effect_has_ttl_fallback_without_losing_spoiler(self):
        source = inspect.getsource(TelegramEffects.send_catalogued_media)
        self.assertIn('"TTL_MEDIA_INVALID"', source)
        self.assertIn("build_media(None)", source)
        self.assertIn("media.spoiler = bool(spoiler)", source)


if __name__ == "__main__":
    unittest.main()
