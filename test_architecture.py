"""Architecture regression tests for the modular-monolith composition root."""

import ast
import inspect
import unittest
from pathlib import Path

from gr_observer.application import Observer
from gr_observer.catalog import COMMANDS, MODULES
from gr_observer.schema import SCHEMA


ROOT = Path(__file__).resolve().parent


class ArchitectureTests(unittest.TestCase):
    def test_group_reply_is_first_class_catalog_module(self):
        self.assertIn("group_reply", MODULES)
        self.assertEqual(MODULES["group_reply"]["dispatch_order"], 25)
        for command_id in (
            "group_reply.enable",
            "group_reply.disable",
            "group_reply.preview",
        ):
            self.assertIn(command_id, COMMANDS)
            self.assertEqual(COMMANDS[command_id]["module"], "group_reply")

    def test_entrypoint_has_no_group_import_side_effect(self):
        tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
        self.assertNotIn("gr_observer.group_integration", imported)

    def test_group_integration_does_not_monkey_patch_runtime(self):
        source = (ROOT / "gr_observer" / "group_integration.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("application.Observer.setup =", source)
        self.assertNotIn("application.ControlPanel =", source)
        self.assertNotIn("MODULES.setdefault", source)
        self.assertNotIn("COMMANDS.setdefault", source)

    def test_composition_root_registers_group_rib_explicitly(self):
        source = inspect.getsource(Observer.setup)
        self.assertIn("register_group_reply(self)", source)
        self.assertIn("GroupControlPanel(self)", source)

    def test_group_schema_is_owned_by_central_schema(self):
        self.assertIn("VALUES('group_reply', FALSE", SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS group_reply_events", SCHEMA)
        self.assertIn("CREATE TABLE IF NOT EXISTS group_repost_state", SCHEMA)

    def test_session_status_requires_authorization_not_client_object_only(self):
        source = inspect.getsource(Observer.status_text)
        self.assertIn("session_authorized", source)
        self.assertNotIn('"online" if self.user is not None', source)


if __name__ == "__main__":
    unittest.main()
