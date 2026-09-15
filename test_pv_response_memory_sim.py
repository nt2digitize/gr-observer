"""Offline simulation tests for the lightweight PV response memory."""

import unittest

from gr_observer.pv_response_memory_sim import (
    PvResponseMemorySimulator,
    classify_intent,
)


class IntentClassificationTests(unittest.TestCase):
    def test_price_variants_collapse_to_one_intent(self):
        for text in (
            "quanto custa?",
            "qual o valor do vip?",
            "qnt tá o vip",
            "preço?",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_intent(text), "price")

    def test_other_small_catalog_intents(self):
        self.assertEqual(classify_intent("aceita pix?"), "payment")
        self.assertEqual(classify_intent("manda o link"), "access")
        self.assertEqual(classify_intent("ela tá ao vivo?"), "live")
        self.assertEqual(classify_intent("tem prévia?"), "preview")
        self.assertEqual(classify_intent("tem conteúdo novo?"), "new_content")
        self.assertEqual(classify_intent("fala comigo"), "unknown")


class ResponseMemorySimulationTests(unittest.TestCase):
    def test_inbound_text_is_reduced_to_intent_only(self):
        memory = PvResponseMemorySimulator()
        self.assertEqual(memory.observe_inbound(10, "qual o valor do vip?"), "price")
        self.assertEqual(memory.pending_intent, {10: "price"})

    def test_automatic_outbound_never_teaches_memory(self):
        memory = PvResponseMemorySimulator()
        memory.observe_inbound(10, "quanto custa?")
        learned = memory.observe_outbound(
            10,
            "quer que eu mande o link?",
            origin="automation",
        )
        self.assertIsNone(learned)
        self.assertEqual(memory.responses_for("price"), [])

    def test_human_reply_becomes_candidate_memory(self):
        memory = PvResponseMemorySimulator()
        memory.observe_inbound(10, "quanto custa?")
        learned = memory.observe_outbound(10, "hoje tá 30", origin="human")
        self.assertIsNotNone(learned)
        self.assertEqual(learned.intent, "price")
        self.assertEqual(learned.status, "new")
        self.assertEqual(learned.times_seen, 1)

    def test_repeated_human_answer_strengthens_same_memory(self):
        memory = PvResponseMemorySimulator()
        for user_id, question in (
            (10, "quanto custa?"),
            (11, "qual o valor?"),
            (12, "qnt tá?"),
        ):
            memory.observe_inbound(user_id, question)
            memory.observe_outbound(user_id, "hoje tá 30", origin="human")

        learned = memory.responses_for("price")
        self.assertEqual(len(learned), 1)
        self.assertEqual(learned[0].times_seen, 3)
        self.assertEqual(learned[0].status, "reliable")

    def test_distinct_human_answers_remain_distinct_candidates(self):
        memory = PvResponseMemorySimulator()
        memory.observe_inbound(10, "quanto custa?")
        memory.observe_outbound(10, "hoje tá 30", origin="human")
        memory.observe_inbound(11, "qual o valor?")
        memory.observe_outbound(11, "30 hoje", origin="human")

        learned = memory.responses_for("price")
        self.assertEqual(len(learned), 2)
        self.assertEqual({entry.response_text for entry in learned}, {"hoje tá 30", "30 hoje"})

    def test_unknown_intent_does_not_learn(self):
        memory = PvResponseMemorySimulator()
        memory.observe_inbound(10, "fala comigo")
        learned = memory.observe_outbound(10, "claro", origin="human")
        self.assertIsNone(learned)
        self.assertEqual(memory.entries, {})


if __name__ == "__main__":
    unittest.main()
