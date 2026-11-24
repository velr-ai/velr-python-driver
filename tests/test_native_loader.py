"""Native loader failures remain actionable without a working engine.

Run independently of the engine-backed fixtures with:
python python/velrpy/tests/test_native_loader.py
"""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


DRIVER_ROOT = Path(__file__).resolve().parents[1]


class NativeLoaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.loader = self.root / "_ffi.py"
        shutil.copyfile(DRIVER_ROOT / "src/velr/_ffi.py", self.loader)

    def failed_import(self):
        result = subprocess.run(
            [sys.executable, str(self.loader)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        project = tomllib.loads((DRIVER_ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["name"], "velr")
        self.assertIn(
            f"python -m pip install --force-reinstall {project['name']}", result.stderr,
        )
        self.assertIn("velr: failed to locate/load the native library", result.stderr)
        self.assertIn(str((self.root / "_vendor").resolve()), result.stderr)
        self.assertNotIn("velrpy", result.stderr)
        return result.stderr

    def test_missing_bundled_library_recommends_the_published_distribution(self):
        error = self.failed_import()
        self.assertIn("<no files present>", error)
        self.assertNotIn("Last dlopen error:", error)

    def test_unloadable_bundled_library_keeps_the_loader_diagnostic(self):
        if sys.platform.startswith("darwin"):
            name = "libvelrc.dylib"
        elif sys.platform.startswith("win"):
            name = "velrc.dll"
        else:
            name = "libvelrc.so"
        vendor = self.root / "_vendor"
        vendor.mkdir()
        candidate = vendor / name
        candidate.write_bytes(b"invalid native library")

        error = self.failed_import()
        self.assertIn(str(candidate.resolve()), error)
        self.assertIn("Last dlopen error:", error)
        self.assertNotIn("<no files present>", error)


if __name__ == "__main__":
    unittest.main()
