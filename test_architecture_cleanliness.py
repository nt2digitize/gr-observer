import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent
PACKAGE = ROOT / "gr_observer"


class ArchitectureCleanlinessTests(unittest.TestCase):
    def test_no_temporary_operational_artifacts(self):
        self.assertFalse(list(ROOT.glob(".deploy-trigger*")))
        self.assertFalse(list((ROOT / "docs").glob("ROLLBACK-*.md")))

    def test_hotfix_overlay_files_are_gone(self):
        forbidden = {
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

    def test_entrypoint_has_no_production_mixin_stack(self):
        text = (ROOT / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("ProductionSafetyMixin", text)
        self.assertNotIn("PvProductionGuardMixin", text)
        self.assertNotIn("DurablePeerObserverMixin", text)
        self.assertNotIn("FloodAwareObserverMixin", text)


if __name__ == "__main__":
    unittest.main()
