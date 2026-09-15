import unittest

from gr_observer.group_intent import classify_group_intent


class GroupIntentStrongTests(unittest.TestCase):
    def assertStrong(self, text):
        decision = classify_group_intent(text)
        self.assertTrue(decision.should_reply, text)
        self.assertEqual(decision.confidence, "strong", text)
        self.assertEqual(decision.intent, "partner_request", text)

    def test_self_contained_requests_are_strong(self):
        cases = (
            "quem quer ver uma esposa?",
            "quero ver sua esposa",
            "quer ver minha esposa?",
            "querem ver uma esposa",
            "quero conhecer sua mulher",
            "mostra sua esposa",
            "mostre a mulher",
            "manda sua esposa",
            "envia a parceira",
            "manda foto da sua esposa",
            "mostra video da sua mulher",
            "tem foto da esposa",
            "tem esposa aí?",
            "tem mulher aqui",
            "cadê sua esposa?",
            "cadê a mulher",
            "esposa no pv",
            "mulher no privado",
            "PARCEIRA NO PV",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertStrong(text)

    def test_accents_and_punctuation_do_not_change_decision(self):
        self.assertStrong("TEM VÍDEO DA SUA ESPOSA???")
        self.assertStrong("Cadê   sua   esposa!!!")


class GroupIntentContextTests(unittest.TestCase):
    def test_contextual_phrases_do_nothing_without_context(self):
        cases = (
            "quero ver",
            "quero conhecer",
            "manda foto",
            "manda vídeo",
            "mostra ela",
            "cadê ela",
            "e a sua?",
        )
        for text in cases:
            with self.subTest(text=text):
                decision = classify_group_intent(text)
                self.assertFalse(decision.should_reply)
                self.assertEqual(decision.confidence, "contextual")
                self.assertEqual(decision.intent, "ambiguous")

    def test_contextual_phrases_activate_with_objective_relevant_reply(self):
        cases = (
            "quero ver",
            "quero conhecer",
            "manda foto",
            "manda video",
            "mostra ela",
            "cade ela",
            "a sua",
        )
        for text in cases:
            with self.subTest(text=text):
                decision = classify_group_intent(text, reply_to_relevant=True)
                self.assertTrue(decision.should_reply)
                self.assertEqual(decision.confidence, "contextual")
                self.assertEqual(decision.intent, "partner_request")

    def test_managed_reply_is_also_valid_context(self):
        decision = classify_group_intent("manda foto", reply_to_managed=True)
        self.assertTrue(decision.should_reply)
        self.assertEqual(decision.confidence, "contextual")


class GroupIntentWeakTests(unittest.TestCase):
    def test_weak_phrases_do_nothing_on_their_own(self):
        cases = ("manda", "manda aí", "quero", "sim", "pode", "bora", "cadê", "mostra")
        for text in cases:
            with self.subTest(text=text):
                decision = classify_group_intent(text)
                self.assertFalse(decision.should_reply)
                self.assertEqual(decision.confidence, "weak")
                self.assertEqual(decision.intent, "ambiguous")

    def test_weak_phrases_require_reply_to_our_managed_message(self):
        for text in ("manda", "quero", "sim", "cadê", "mostra"):
            with self.subTest(text=text):
                decision = classify_group_intent(text, reply_to_managed=True)
                self.assertTrue(decision.should_reply)
                self.assertEqual(decision.confidence, "weak")
                self.assertEqual(decision.intent, "partner_request")

    def test_generic_relevant_context_is_not_enough_for_weak_phrase(self):
        decision = classify_group_intent("sim", reply_to_relevant=True)
        self.assertFalse(decision.should_reply)
        self.assertEqual(decision.confidence, "weak")


class GroupIntentNegativeAndFalsePositiveTests(unittest.TestCase):
    def test_explicit_negative_intent_always_wins(self):
        cases = (
            "não quero ver sua esposa",
            "nao manda foto da sua esposa",
            "não mostra sua mulher",
            "pare de mandar",
            "para de responder",
        )
        for text in cases:
            with self.subTest(text=text):
                decision = classify_group_intent(
                    text,
                    reply_to_managed=True,
                    reply_to_relevant=True,
                )
                self.assertFalse(decision.should_reply)
                self.assertEqual(decision.confidence, "negative")
                self.assertEqual(decision.intent, "negative")

    def test_unrelated_conversation_does_not_trigger(self):
        cases = (
            "minha esposa foi ao mercado",
            "falei com minha mulher ontem",
            "minha esposa quer entrar no grupo",
            "mulher bonita",
            "esposa do administrador",
            "manda o endereço",
            "manda foto do carro",
            "quero ver o jogo",
            "quero ver se funciona",
            "mostra o documento",
            "cadê o grupo",
            "quem quer pizza?",
            "tem mulher trabalhando aqui",
            "video do carro",
            "foto da praia",
            "bom dia pessoal",
            "kkkk",
        )
        for text in cases:
            with self.subTest(text=text):
                decision = classify_group_intent(text)
                self.assertFalse(decision.should_reply, (text, decision))
                self.assertEqual(decision.confidence, "none", (text, decision))

    def test_empty_text_is_none(self):
        decision = classify_group_intent("   ")
        self.assertFalse(decision.should_reply)
        self.assertEqual(decision.rule, "empty")


class GroupIntentExplainabilityTests(unittest.TestCase):
    def test_every_actionable_decision_has_named_rule(self):
        decision = classify_group_intent("manda foto da sua esposa")
        self.assertTrue(decision.should_reply)
        self.assertTrue(decision.rule.startswith("strong."))

    def test_classifier_is_deterministic(self):
        first = classify_group_intent("manda", reply_to_managed=True)
        for _ in range(100):
            self.assertEqual(first, classify_group_intent("manda", reply_to_managed=True))


if __name__ == "__main__":
    unittest.main()
