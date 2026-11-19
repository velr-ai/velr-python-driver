"""Standard-library regression checks for the installed-wheel documentation runner."""

import ast
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_doc_examples.py"
SPEC = importlib.util.spec_from_file_location("velr_doc_examples_checker", SCRIPT)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class DocumentationExampleChecks(unittest.TestCase):
    def test_unicode_source_examples_are_utf8_with_normalized_newlines(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "example.py").write_bytes("print('Malmö → graph')\r\n".encode("utf-8"))
            examples = checker.discover_source_examples(root)
            self.assertEqual(examples[0]["code"], "print('Malmö → graph')\n")

    def test_isolated_children_use_utf8_for_output_and_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            code = ("import sys; assert sys.flags.isolated and sys.flags.utf8_mode; "
                    "print('Malmö → graph'); print('╰── diagnostic', file=sys.stderr)")
            result = checker.run_process([*checker.ISOLATED_PYTHON, "-c", code], Path(directory))
            self.assertEqual(result["returncode"], 0, result)
            self.assertEqual(result["stdout"], "Malmö → graph\n")
            self.assertEqual(result["stderr"], "╰── diagnostic\n")

    def test_wheel_preserves_crlf_examples_and_rejects_changed_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "package"
            (package / "examples").mkdir(parents=True)
            (package / "src/velr").mkdir(parents=True)
            files = {"velr/__init__.py": b'"""Module documentation."""\r\n'}
            (package / "src/velr/__init__.py").write_bytes(files["velr/__init__.py"])
            prefix = "velr-0.0.0.data/data/share/doc/velr/"
            for document in checker.DOCUMENTS:
                content = b"Reference\r\n"
                (package / document).write_bytes(content)
                files[prefix + document] = content
            script = "print('Malmö → graph')\r\n".encode("utf-8")
            (package / "examples/basic_query.py").write_bytes(script)
            files[prefix + "examples/basic_query.py"] = script
            wheel = root / "velr-0.0.0-py3-none-any.whl"

            def write_wheel():
                with zipfile.ZipFile(wheel, "w") as archive:
                    for name, data in files.items():
                        archive.writestr(name, data)

            write_wheel()
            self.assertEqual(checker.verify_wheel(wheel, package)["path"], str(wheel))
            files[prefix + "examples/basic_query.py"] = b"print('stale')\r\n"
            write_wheel()
            with self.assertRaisesRegex(ValueError, "stale or missing: examples/basic_query.py"):
                checker.verify_wheel(wheel, package)

    def test_fence_inventory_tracks_all_languages_and_stable_headings(self):
        text = "# Intro\n```sh\npip install velr\n```\n## Query **values**\n```python\nprint(1)\n```\n```cypher\nRETURN 1 AS value\n```\n"
        blocks = checker.extract_fences(text, "README.md")
        self.assertEqual([(block["id"], block["language"]) for block in blocks], [
            ("README.md#intro:1", "sh"), ("README.md#query-values:1", "python"),
            ("README.md#query-values:2", "cypher")])
        self.assertEqual(checker.extract_fences("Preface\n" + text, "README.md")[1]["id"], blocks[1]["id"])
        self.assertEqual(blocks[1]["code"], "print(1)\n")
        with self.assertRaisesRegex(ValueError, "unterminated"):
            checker.extract_fences("```python\nprint(1)\n", "GUIDE.md")

    def test_rst_literal_examples_need_no_injected_context(self):
        blocks = checker.extract_usage("Use a connection::\n\n    from velr.driver import Velr\n\n    with Velr.open() as db:\n        assert db.to_records('RETURN 1 AS n') == [{'n': 1}]\n\nOther prose.\n", "docstring:velr.driver")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["id"], "docstring:velr.driver#literal:1")
        self.assertTrue(blocks[0]["code"].startswith("from velr.driver import Velr\n"))
        compile(blocks[0]["code"], blocks[0]["id"], "exec")

    def test_rst_markers_inside_fences_do_not_create_duplicate_examples(self):
        text = '```python\ntext = "example::"\nif True:\n    print(text)\n```\n'
        self.assertEqual(len(checker.extract_usage(text, "GUIDE.md")), 1)

    def test_explicit_code_directives_and_pycon_fences_are_not_missed(self):
        directive = ".. code-block:: python\n\n    print(42)\n"
        blocks = checker.extract_usage(directive, "docstring:example")
        self.assertEqual(blocks[0]["code"], "print(42)\n")
        repl = checker.extract_usage("```pycon\n>>> print(42)\n42\n```\n", "GUIDE.md")
        self.assertEqual(len(repl), 1)
        self.assertEqual(repl[0]["doctest"], ">>> print(42)\n42\n")

    def test_repl_sessions_keep_expected_output_and_shared_namespace(self):
        text = ">>> values = [1, 2]\n>>> sum(values)\n3\n"
        blocks = checker.extract_usage(text, "docstring:example")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["source_kind"], "repl")
        self.assertEqual(blocks[0]["doctest"], text)

    def test_new_source_examples_cannot_escape_classification(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "first.py").write_text("print(1)\n")
            (root / "nested/second.py").write_text("print(2)\n")
            (root / "ignored.ipynb").write_text("excluded notebook")
            programs = checker.discover_source_examples(root)
            self.assertEqual([program["id"] for program in programs], ["examples/first.py", "examples/nested/second.py"])
            entries = {program["id"]: {"language": "python", "kind": "run", "checks": [{"kind": "output", "match": [1]}]} for program in programs}
            checker.validate_inventory(programs, {"examples": entries})
            (root / "new.py").write_text("print(3)\n")
            with self.assertRaisesRegex(ValueError, "Unclassified.*examples/new.py"):
                checker.validate_inventory(checker.discover_source_examples(root), {"examples": entries})

    def test_programs_cannot_be_skipped_or_lose_assertions(self):
        blocks = checker.extract_fences("```python\nprint(42)\n```\n", "README.md")
        entry = {"language": "python", "kind": "reference", "reason": "incorrect skip"}
        with self.assertRaisesRegex(ValueError, "cannot be skipped"):
            checker.validate_inventory(blocks, {"examples": {blocks[0]["id"]: entry}})
        entry["kind"] = "run"
        with self.assertRaisesRegex(ValueError, "behavior assertions"):
            checker.validate_inventory(blocks, {"examples": {blocks[0]["id"]: entry}})
        entry["inline_assertions"] = True
        with self.assertRaisesRegex(ValueError, "No inline assert"):
            checker.validate_inventory(blocks, {"examples": {blocks[0]["id"]: entry}})

    def test_stale_classifications_and_signature_bypasses_fail(self):
        with self.assertRaisesRegex(ValueError, "stale classifications"):
            checker.validate_inventory([], {"examples": {"old": {}}})
        block = checker.extract_fences("```python\nprint(1)\n```\n", "API_REFERENCE.md")[0]
        with self.assertRaisesRegex(ValueError, "Only verified generated"):
            checker.validate_inventory([block], {"examples": {block["id"]: {"language": "python", "kind": "generated-reference"}}})

    def test_api_signatures_and_text_are_verified_against_public_ast(self):
        source = '"""Module documentation."""\nclass Velr:\n    """Connection documentation."""\n    def run(self, query: str) -> None:\n        """Execute a query."""\n        pass\n'
        api = "## velr.driver\n\nModule documentation.\n\n### velr.driver.Velr\n\nConnection documentation.\n\n#### velr.driver.Velr.run\n\n```python\nrun(query: str) -> None\n```\n\nExecute a query.\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "driver.py"
            path.write_text(source)
            blocks = checker.extract_usage(api, "API_REFERENCE.md")
            auto = checker.generated_signatures(api, blocks, path)
            self.assertEqual(list(auto), ["API_REFERENCE.md#velrdrivervelrrun:1"])
            with self.assertRaisesRegex(ValueError, "stale or missing"):
                checker.generated_signatures(api.replace("Execute a query.", "Old text."), blocks, path)

    def test_meaningful_output_checks_keep_counts_numeric_bounds_and_exact_integers(self):
        records = [{"level": "stdout", "args": [[{"title": "Graph", "score": 0.75, "id": 9007199254740993}]]}]
        checks = [{"kind": "output", "match": [[{"title": "Graph", "score": {"$number": {"min": 0, "max": 1}}, "id": 9007199254740993}]]}]
        checker.verify_checks(checks, records, "")
        self.assertFalse(checker.matches(float("nan"), {"$number": {}}))
        self.assertFalse(checker.matches(True, 1))
        self.assertFalse(checker.matches([1, 2], {"$array_contains": [1], "$length": 1}))
        with self.assertRaisesRegex(AssertionError, "Expected output"):
            checker.verify_checks([{**checks[0], "count": 2}], records, "")

    def test_public_assignment_aliases_keep_renamed_reference_signatures(self):
        source = ast.parse("class Connection:\n    def run(self, query: str) -> None:\n        pass\n    execute = run\n")
        methods = list(checker.class_methods(source.body[0]))
        self.assertEqual([method.name for method in methods], ["run", "execute"])
        self.assertEqual(checker.signature(methods[1], method=True), "execute(query: str) -> None")
        self.assertEqual(source.body[0].body[0].name, "run")

    def test_cypher_fixture_uses_parameterized_execution_and_closes_all_result_tables(self):
        source = checker.cypher_program({"code": "RETURN $value AS value"}, {"params": {"value": 42}})
        compile(source, "query-reference-wrapper", "exec")
        self.assertIn("with db.exec(", source)
        self.assertIn("params={'value': 42}", source)
        self.assertIn("for table in stream.iter_tables():", source)
        self.assertIn("with table:", source)
        self.assertNotIn("db.to_records('RETURN", source)


if __name__ == "__main__":
    unittest.main()
