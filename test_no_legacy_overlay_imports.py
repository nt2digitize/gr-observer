from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parent


class NoLegacyOverlayImportTests(unittest.TestCase):
    def test_python_sources_do_not_import_removed_overlay_modules(self):
        forbidden = (
            "runtime_safety",
            "durable_peers",
            "pv_production_guard",
            "pv_editor_safety",
        )
        offenders = []
        for path in ROOT.rglob("*.py"):
            if path.name == "test_no_legacy_overlay_imports.py":
                continue
            text = path.read_text(encoding="utf-8")
            for name in forbidden:
                if f".{name}" in text or f"gr_observer.{name}" in text:
                    offenders.append(f"{path.relative_to(ROOT)} -> {name}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
