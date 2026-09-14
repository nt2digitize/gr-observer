import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "gr_observer"


class ArchitectureCleanlinessTests(unittest.TestCase):
    def test_no_temporary_operational_artifacts(self):
        self.assertFalse(list(ROOT.glob(".deploy-trigger*")))
        self.assertFalse(list((ROOT / "docs").glob("ROLLBACK-*.md")))

    def test_obsolete_overlay_files_are_gone(self):
        forbidden = {
            "runtime_safety.py",
            "durable_peers.py",
            "pv_production_guard.py",
            "pv_editor_safety.py",
        }
        present = {path.name for path in PACKAGE.glob("*.py")}
        self.assertTrue(forbidden.isdisjoint(present), sorted(forbidden & present))

    def test_no_runtime_monkey_patches(self):
        forbidden_fragments = (
            "MethodType(",
            ".get_input_entity =",
            ".begin_effect =",
            ".fail_action =",
            "pv_message_runtime.has_link =",
        )
        offenders = []
        for path in PACKAGE.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for fragment in forbidden_fragments:
                if fragment in text:
                    offenders.append(f"{path.relative_to(ROOT)}: {fragment}")
        self.assertEqual(offenders, [])

    def test_single_user_runtime_owner(self):
        owners = []
        for path in PACKAGE.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "user_runtime":
                    owners.append(str(path.relative_to(ROOT)))
        self.assertEqual(owners, ["gr_observer/application.py"])

    def test_single_user_client_and_writer_are_constructed_in_that_runtime(self):
        tree = ast.parse((PACKAGE / "application.py").read_text(encoding="utf-8"))
        runtime = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "user_runtime"
        )
        calls = []
        for node in ast.walk(runtime):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                calls.append(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                calls.append(node.func.attr)
        self.assertEqual(calls.count("DurableTelegramClient"), 1)
        self.assertEqual(calls.count("SafeOutboxWriter"), 1)

    def test_entrypoint_is_only_a_thin_observer_launcher(self):
        text = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertIn("asyncio.run(Observer().run())", text)
        self.assertNotIn("RuntimeObserver", text)
        self.assertNotIn("Mixin", text)

    def test_governor_and_peer_deferral_never_delete_persisted_history(self):
        text = "\n".join(
            (PACKAGE / name).read_text(encoding="utf-8")
            for name in ("traffic.py", "telegram_peers.py")
        ).upper()
        self.assertNotIn("DELETE FROM", text)
        self.assertNotIn("TRUNCATE", text)
        self.assertNotIn("DROP TABLE", text)

    def test_radar_production_policy_cannot_start_periodic_scan(self):
        text = (PACKAGE / "modules" / "radar_passive.py").read_text(encoding="utf-8")
        self.assertIn("self.scan_task = None", text)
        self.assertNotIn("CREATE_TASK", text.upper())
        self.assertNotIn("SCAN_LOOP", text.upper())


if __name__ == "__main__":
    unittest.main()
