"""Regression tests for the optional two-screens preference branch."""

import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from gr_observer.catalog import CAMPAIGNS
from gr_observer.modules.pv_reply import (
    PvReplyModule,
    classify_two_screens_choice,
    classify_two_screens_response,
)


class TwoScreensChoiceTests(unittest.IsolatedAsyncioTestCase):
    def test_yes_and_plain_no_both_continue_to_preference(self):
        self.assertEqual(classify_two_screens_response("sim"), "positive")
        self.assertEqual(classify_two_screens_response("não"), "positive")
        self.assertEqual(classify_two_screens_response("nao"), "positive")

    def test_real_stop_request_is_still_an_opt_out(self):
        self.assertEqual(classify_two_screens_response("parar"), "opt_out")
        self.assertEqual(classify_two_screens_response("não envie"), "opt_out")

    def test_preference_copy_is_exact_and_retry_repeats_options(self):
        expected = "Onde quer jogar um leite? Peito, buceta ou cuzinho?"
        self.assertEqual(CAMPAIGNS["pv.two_screens.question"]["text"], expected)
        self.assertEqual(CAMPAIGNS["pv.two_screens.retry"]["text"], expected)

    def test_choice_classifier_accepts_natural_aliases(self):
        cases = {
            "peito": "peitos",
            "nas tetas": "peitos",
            "na xereca": "buceta",
            "xana": "buceta",
            "ppk": "buceta",
            "cuzinho": "cu",
            "no rabinho": "cu",
            "bunda": "cu",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify_two_screens_choice(text), expected)

    def test_last_explicit_preference_wins_when_user_changes_mind(self):
        self.assertEqual(
            classify_two_screens_choice("peito ou cu... melhor no cuzinho"),
            "cu",
        )
        self.assertEqual(
            classify_two_screens_choice("buceta ou peito, prefiro peito"),
            "peitos",
        )

    def test_short_alias_does_not_match_inside_unrelated_word(self):
        self.assertIsNone(classify_two_screens_choice("quero curtir"))

    async def test_question_immediately_opens_choice_state(self):
        storage = NS(
            two_screens_action_allowed=AsyncMock(return_value=True),
            advance_two_screens=AsyncMock(return_value=True),
            queue_next_two_screens_action=AsyncMock(),
        )
        module = PvReplyModule(storage, NS(pv_preview_link="https://example.test"))
        effects = NS(send_text=AsyncMock(return_value={"message_id": 12}))
        action = {"action_key": "two-question", "payload": {"peer": 42}}

        result = await module.action_send_two_screens_question(action, effects)

        self.assertTrue(result["sent"])
        effects.send_text.assert_awaited_once_with(
            42,
            "Onde quer jogar um leite? Peito, buceta ou cuzinho?",
            "two-question:send",
        )
        storage.advance_two_screens.assert_awaited_once_with(
            42, "question_queued", "awaiting_choice"
        )
        storage.queue_next_two_screens_action.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
