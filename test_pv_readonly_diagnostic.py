from pathlib import Path
import unittest


class PvReadonlyDiagnosticContractTests(unittest.TestCase):
    def setUp(self):
        self.source = Path("scripts/pv_readonly_diagnostic.py").read_text(encoding="utf-8")

    def test_database_connection_and_transaction_are_read_only(self):
        self.assertIn('"default_transaction_read_only": "on"', self.source)
        self.assertIn("transaction(readonly=True)", self.source)

    def test_tool_has_no_database_mutation_statements(self):
        upper = self.source.upper()
        for token in (
            "INSERT INTO",
            "UPDATE ",
            "DELETE FROM",
            "ALTER TABLE",
            "CREATE TABLE",
            "DROP TABLE",
            "TRUNCATE ",
        ):
            self.assertNotIn(token, upper)

    def test_tool_has_no_telegram_or_writer_runtime(self):
        for token in (
            "TelegramClient",
            "USER_SESSION_STRING",
            "SafeOutboxWriter",
            "TrafficGovernor",
            "send_message(",
            "send_file(",
        ):
            self.assertNotIn(token, self.source)

    def test_report_does_not_select_message_content_or_full_payloads(self):
        self.assertNotIn("m.content", self.source)
        self.assertNotIn("SELECT payload", self.source)
        self.assertNotIn("preview_link", self.source)


if __name__ == "__main__":
    unittest.main()
