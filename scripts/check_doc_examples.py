#!/usr/bin/env python3
"""Inventory public Python examples and execute them against an installed wheel."""

from __future__ import annotations

import argparse
import ast
import copy
import doctest
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import zipfile

PACKAGE = Path(__file__).resolve().parents[1]
MANIFEST = Path(__file__).with_name("doc_examples.json")
DOCUMENTS = ["README.md", "GUIDE.md", "API_REFERENCE.md", "QUERY_LANGUAGE.md"]
PYTHON_LANGUAGES = {"python", "py", "python3", "pycon"}
PROTOCOLS = {"__enter__", "__exit__", "__iter__", "__next__"}
ISOLATED_PYTHON = (sys.executable, "-I", "-X", "utf8")


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def slug(text: str) -> str:
    return re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", text.lower()))


def extract_fences(markdown: str, document: str) -> list[dict]:
    lines = markdown.splitlines()
    examples = []
    seen = {}
    ordinals = {}
    heading, heading_text, heading_level = "introduction", "Introduction", 0
    index = 0
    while index < len(lines):
        title = re.match(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$", lines[index])
        if title:
            heading_text = title[2]
            heading_level = len(title[1])
            base = slug(heading_text)
            number = seen.get(base, 0)
            seen[base] = number + 1
            heading = f"{base}-{number}" if number else base
        opening = re.match(r"^ {0,3}(`{3,}|~{3,})\s*(\S*)\s*(.*?)\s*$", lines[index])
        if not opening:
            index += 1
            continue
        marker, language, info = opening.groups()
        start = index + 1
        code = []
        index += 1
        while index < len(lines):
            closing = re.match(r"^ {0,3}(`{3,}|~{3,})\s*$", lines[index])
            if closing and closing[1][0] == marker[0] and len(closing[1]) >= len(marker):
                break
            code.append(lines[index])
            index += 1
        if index == len(lines):
            raise ValueError(f"{document}:{start}: unterminated code fence")
        ordinal = ordinals.get(heading, 0) + 1
        ordinals[heading] = ordinal
        source = "\n".join(code) + "\n"
        examples.append({"id": f"{document}#{heading}:{ordinal}", "source_kind": "fence",
                         "document": document, "line": start, "end_line": index + 1,
                         "heading": heading_text, "heading_level": heading_level,
                         "language": language.lower(), "info": info, "code": source,
                         "sha256": digest(source)})
        index += 1
    return examples


def extract_literals(text: str, document: str) -> list[dict]:
    """Read explicit RST literal blocks outside Markdown fences, with no hidden namespace."""
    fenced = {line for block in extract_fences(text, document)
              for line in range(block["line"], block["end_line"] + 1)}
    lines = text.splitlines()
    examples = []
    index = 0
    while index < len(lines):
        marker = lines[index]
        directive = re.search(r"\.\. (?:code-block|code)::\s*(\w+)\s*$", marker)
        if index + 1 in fenced or not (marker.rstrip().endswith("::") or directive):
            index += 1
            continue
        base_indent = len(marker) - len(marker.lstrip())
        start = index + 1
        while start < len(lines) and not lines[start].strip():
            start += 1
        if start == len(lines) or len(lines[start]) - len(lines[start].lstrip()) <= base_indent:
            index += 1
            continue
        end = start
        while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > base_indent):
            end += 1
        code = textwrap.dedent("\n".join(lines[start:end])).rstrip() + "\n"
        if not code.lstrip().startswith(">>>"):
            examples.append({"id": f"{document}#literal:{len(examples) + 1}", "source_kind": "literal",
                             "document": document, "line": start + 1,
                             "language": directive[1] if directive else "python", "code": code,
                             "sha256": digest(code)})
        index = end
    return examples


def extract_usage(text: str, document: str) -> list[dict]:
    examples = extract_fences(text, document) + extract_literals(text, document)
    fenced_lines = set()
    for example in examples:
        if example["source_kind"] == "fence":
            fenced_lines.update(range(example["line"], example["end_line"] + 1))
            if example["language"] == "pycon":
                example["doctest"] = example["code"]
    unfenced = "\n".join(line if index + 1 not in fenced_lines else "" for index, line in enumerate(text.splitlines()))
    if text.endswith("\n"):
        unfenced += "\n"
    repl = doctest.DocTestParser().get_examples(unfenced)
    if repl:
        code = "\n".join(example.source.rstrip() for example in repl) + "\n"
        examples.append({"id": f"{document}#repl:1", "source_kind": "repl", "document": document,
                         "line": repl[0].lineno + 1, "language": "python", "code": code,
                         "doctest": unfenced, "sha256": digest(unfenced)})
    return examples


def public(name: str) -> bool:
    return not name.startswith("_") or name in PROTOCOLS


def class_methods(node: ast.ClassDef):
    definitions = {member.name: member for member in node.body
                   if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for member in node.body:
        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield member
        elif isinstance(member, ast.Assign) and len(member.targets) == 1 and isinstance(member.targets[0], ast.Name) and isinstance(member.value, ast.Name) and member.value.id in definitions:
            alias = copy.deepcopy(definitions[member.value.id])
            alias.name = member.targets[0].id
            yield alias


def source_docstrings(path: Path, module: str) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    result = {}
    if doc := ast.get_docstring(tree):
        result[module] = doc
    for node in tree.body:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        doc = ast.get_docstring(node)
        if not public(node.name) or doc and "@private" in doc:
            continue
        name = f"{module}.{node.name}"
        if doc:
            result[name] = doc
        if isinstance(node, ast.ClassDef):
            for member in class_methods(node):
                method_doc = ast.get_docstring(member)
                if (public(member.name) or member.name == "__init__" and method_doc) and method_doc and "@private" not in method_doc:
                    result[f"{name}.{member.name}"] = method_doc
    return result


def discover_source_examples(directory: Path) -> list[dict]:
    result = []
    for path in sorted(directory.rglob("*.py")):
        document = "examples/" + path.relative_to(directory).as_posix()
        code = path.read_text(encoding="utf-8")
        result.append({"id": document, "source_kind": "file", "document": document,
                       "line": 1, "language": "python", "code": code, "sha256": digest(code)})
    return result


def signature(node, method=False) -> str:
    decorators = [ast.unparse(value) for value in node.decorator_list]
    if "property" in decorators:
        return f"{node.name}: {ast.unparse(node.returns) if node.returns else 'Any'}"
    arguments = copy.deepcopy(node.args)
    if method:
        positional = arguments.posonlyargs or arguments.args
        if positional and positional[0].arg in {"self", "cls"}:
            positional.pop(0)
    annotation = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    decoration = "".join(f"@{value}\n" for value in decorators if value in {"classmethod", "staticmethod"})
    return decoration + f"{node.name}({ast.unparse(arguments)}){annotation}"


def generated_signatures(api: str, blocks: list[dict], source: Path) -> dict[str, dict]:
    """Verify reference prose/signatures against the AST before classifying declarations."""
    expected = {}
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) or not public(node.name):
            continue
        if "@private" in (ast.get_docstring(node) or ""):
            continue
        name = f"velr.driver.{node.name}"
        if isinstance(node, ast.ClassDef):
            for member in class_methods(node):
                if public(member.name) or member.name == "__init__" and ast.get_docstring(member):
                    if "@private" not in (ast.get_docstring(member) or ""):
                        expected[f"{name}.{member.name}"] = signature(member, method=True)
        else:
            expected[name] = signature(node)
    headings = list(re.finditer(r"^#{2,4} (.+)$", api, re.M))
    sections = {match[1]: api[match.end():headings[index + 1].start() if index + 1 < len(headings) else len(api)]
                for index, match in enumerate(headings)}
    for name, doc in source_docstrings(source, "velr.driver").items():
        normalized = "\n".join(line.rstrip() for line in doc.strip().splitlines())
        if normalized not in sections.get(name, ""):
            raise ValueError(f"API_REFERENCE.md has stale or missing docstring text for {name}")
    result = {}
    verified = set()
    for block in blocks:
        name = block.get("heading")
        if block["document"] == "API_REFERENCE.md" and name in expected and block["code"].strip() == expected[name]:
            result[block["id"]] = {"language": "python", "kind": "generated-reference",
                                    "reason": "Reference declaration matches the current public AST; usage examples are executed separately."}
            verified.add(name)
    if verified != set(expected):
        raise ValueError(f"API_REFERENCE.md has stale or missing signatures: {sorted(set(expected) - verified)}")
    return result


def matches(actual, expected) -> bool:
    if isinstance(expected, dict):
        if "$number" in expected:
            bounds = expected["$number"]
            return isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual) and ("min" not in bounds or actual >= bounds["min"]) and ("max" not in bounds or actual <= bounds["max"])
        if "$contains" in expected:
            return isinstance(actual, str) and expected["$contains"] in actual
        if "$regex" in expected:
            return isinstance(actual, str) and re.search(expected["$regex"], actual) is not None
        if "$one_of" in expected:
            return any(matches(actual, candidate) for candidate in expected["$one_of"])
        if "$array_contains" in expected:
            return isinstance(actual, list) and ("$length" not in expected or len(actual) == expected["$length"]) and all(any(matches(value, candidate) for value in actual) for candidate in expected["$array_contains"])
        return isinstance(actual, dict) and all(key in actual and matches(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(matches(left, right) for left, right in zip(actual, expected))
    return type(actual) is type(expected) and actual == expected


def validate_inventory(examples: list[dict], manifest: dict, generated: set[str] = frozenset()) -> None:
    entries = manifest["examples"]
    ids = {example["id"] for example in examples}
    if len(ids) != len(examples):
        raise ValueError("Duplicate example ID")
    unknown = ids - entries.keys()
    stale = entries.keys() - ids
    if unknown or stale:
        raise ValueError(f"Unclassified examples: {sorted(unknown)}; stale classifications: {sorted(stale)}")
    for example in examples:
        entry = entries[example["id"]]
        if entry["language"] != example["language"]:
            raise ValueError(f"Example language changed: {example['id']}")
        if entry["kind"] == "generated-reference":
            if example["id"] not in generated:
                raise ValueError("Only verified generated signatures may be excluded from execution")
            continue
        if example["language"] in PYTHON_LANGUAGES or example["language"] == "cypher":
            if entry["kind"] != "run":
                raise ValueError(f"Executable examples cannot be skipped: {example['id']}")
            if not entry.get("checks") and not entry.get("inline_assertions") and "doctest" not in example:
                raise ValueError(f"Example requires behavior assertions: {example['id']}")
            if example["language"] in PYTHON_LANGUAGES:
                code = doctest.script_from_examples(example["doctest"]) if "doctest" in example else example["code"]
                tree = ast.parse(code, filename=example["id"])
                compile(tree, example["id"], "exec")
                if entry.get("inline_assertions") and not any(isinstance(node, ast.Assert) for node in ast.walk(tree)):
                    raise ValueError(f"No inline assert found: {example['id']}")
        elif entry["kind"] not in {"installation", "configuration", "reference", "shell"} or not entry.get("reason"):
            raise ValueError(f"Nonexecutable examples need an explicit classification: {example['id']}")


def inventory(package: Path, manifest: dict) -> tuple[list[dict], dict]:
    if manifest.get("documents") != DOCUMENTS:
        raise ValueError("Inventory must include README, GUIDE, API_REFERENCE, and QUERY_LANGUAGE")
    examples = []
    for document in DOCUMENTS:
        examples.extend(extract_usage((package / document).read_text(encoding="utf-8"), document))
    examples.extend(discover_source_examples(package / "examples"))
    mirrored = []
    for path in sorted((package / "src/velr").rglob("*.py")):
        if "_vendor" in path.parts:
            continue
        name = path.relative_to(package / "src").with_suffix("").as_posix().replace("/", ".")
        name = name.removesuffix(".__init__")
        docs = source_docstrings(path, name)
        if name in {"velr.guide", "velr.query_language"}:
            canonical = "GUIDE.md" if name == "velr.guide" else "QUERY_LANGUAGE.md"
            extracted = extract_usage(docs.get(name, ""), name)
            expected = [example for example in examples if example["document"] == canonical]
            if [(item["language"], item["code"]) for item in extracted] != [(item["language"], item["code"]) for item in expected]:
                raise ValueError(f"{name} examples are stale relative to {canonical}; regenerate embedded reference modules")
            mirrored.extend({"id": item["id"], "canonical": original["id"], "sha256": original["sha256"]}
                            for item, original in zip(extracted, expected))
        else:
            for owner, doc in docs.items():
                examples.extend(extract_usage(doc, f"docstring:{owner}"))
    auto = generated_signatures((package / "API_REFERENCE.md").read_text(encoding="utf-8"), examples, package / "src/velr/driver.py")
    overlap = auto.keys() & manifest["examples"].keys()
    if overlap:
        raise ValueError(f"Generated signatures cannot obscure explicit examples: {sorted(overlap)}")
    resolved = {**manifest, "examples": {**manifest["examples"], **auto}}
    validate_inventory(examples, resolved, set(auto))
    return examples, {"entries": resolved["examples"], "mirrored": mirrored}


BOOTSTRAP = r'''
import ast, builtins, dataclasses, datetime, doctest, json, math, os, pathlib, runpy, sys
site, payload_path, result_path = map(pathlib.Path, sys.argv[1:4])
sys.path.insert(0, str(site))
os.environ.pop("VELR_IMPORT_LIBRARY", None)
import velr, velr.driver
assert pathlib.Path(velr.__file__).resolve().is_relative_to(site.resolve()), velr.__file__
assert pathlib.Path(velr.driver.__file__).resolve().is_relative_to(site.resolve()), velr.driver.__file__
payload = json.loads(payload_path.read_text(encoding="utf-8"))
records = []
original_print = builtins.print
def normalize(value):
    if value is None or isinstance(value, (str, bool, int)): return value
    if isinstance(value, float): return value if math.isfinite(value) else {"$float": str(value)}
    if isinstance(value, (list, tuple)): return [normalize(item) for item in value]
    if isinstance(value, dict): return {str(key): normalize(item) for key, item in value.items()}
    if isinstance(value, (datetime.date, datetime.time, datetime.timedelta)): return str(value)
    module = type(value).__module__
    if module.startswith("pandas") and hasattr(value, "to_dict"):
        return {"$type": "pandas.DataFrame", "columns": list(value.columns), "records": normalize(value.to_dict(orient="records"))}
    if module.startswith("polars") and hasattr(value, "to_dicts"):
        return {"$type": "polars.DataFrame", "columns": list(value.columns), "records": normalize(value.to_dicts())}
    if module.startswith("pyarrow") and hasattr(value, "to_pylist"):
        return {"$type": "pyarrow.Table", "columns": value.column_names, "records": normalize(value.to_pylist())}
    if hasattr(value, "tolist"): return normalize(value.tolist())
    if dataclasses.is_dataclass(value): return normalize(dataclasses.asdict(value))
    return {"$type": type(value).__qualname__, "repr": repr(value)}
def captured_print(*args, **kwargs):
    records.append({"level": "stderr" if kwargs.get("file") is sys.stderr else "stdout", "args": [normalize(value) for value in args]})
    original_print(*args, **kwargs)
builtins.print = captured_print
try:
    if payload.get("preflight"):
        import numpy, pandas, polars, pyarrow
        if payload.get("importer"):
            import velr_import
            assert pathlib.Path(velr_import.__file__).resolve().is_relative_to(site.resolve()), velr_import.__file__
        with velr.driver.Velr.open() as db:
            assert db.to_records("RETURN 42 AS value") == [{"value": 42}]
        original_print(json.dumps({"velr": velr.__file__, "driver": velr.driver.__file__, "numpy": numpy.__version__, "pandas": pandas.__version__, "polars": polars.__version__, "pyarrow": pyarrow.__version__}))
    elif "doctest" in payload:
        test = doctest.DocTestParser().get_doctest(payload["doctest"], {}, payload["id"], payload["id"], 0)
        for example in test.examples: compile(example.source, payload["id"], "exec")
        result = doctest.DocTestRunner().run(test)
        assert result.attempted and result.failed == 0, result
    else:
        source = pathlib.Path(payload["program"]).read_text(encoding="utf-8")
        compile(source, payload["id"], "exec")
        runpy.run_path(payload["program"], run_name="__main__")
finally:
    pathlib.Path(result_path).write_text(json.dumps(records, indent=2) + "\n", encoding="utf-8")
'''


def run_process(command: list[str], cwd: Path, timeout: float = 45) -> dict:
    start = time.monotonic()
    try:
        completed = subprocess.run(command, cwd=cwd, encoding="utf-8", stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, timeout=timeout)
        return {"returncode": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr,
                "seconds": round(time.monotonic() - start, 3)}
    except subprocess.TimeoutExpired as error:
        def text(value):
            return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""
        return {"returncode": 124, "timeout": True, "stdout": text(error.stdout), "stderr": text(error.stderr),
                "seconds": round(time.monotonic() - start, 3)}


def verify_checks(checks: list[dict], records: list[dict], output: str) -> None:
    for check in checks:
        if check["kind"] == "output":
            found = [record for record in records if ("level" not in check or record["level"] == check["level"])
                     and matches(record["args"], check["match"])]
            if not found or "count" in check and len(found) != check["count"]:
                raise AssertionError(f"Expected output {check}; actual records: {records}")
        elif check["kind"] == "text":
            if "contains" in check and check["contains"] not in output or "regex" in check and not re.search(check["regex"], output):
                raise AssertionError(f"Missing output evidence: {check}")
        else:
            raise ValueError(f"Unknown assertion kind: {check['kind']}")


def cypher_program(example: dict, entry: dict) -> str:
    return ("from velr.driver import Velr\nwith Velr.open('reference.db') as db:\n"
            + "".join(f"    db.run({query!r})\n" for query in entry.get("setup", []))
            + "    results = []\n"
            + f"    with db.exec({example['code']!r}, params={entry.get('params', {})!r}) as stream:\n"
            + "        for table in stream.iter_tables():\n"
            + "            with table:\n"
            + "                results = table.to_records()\n"
            + "    print(results)\n"
            + "".join(f"    print(db.to_records({query!r}))\n" for query in entry.get("verify_queries", [])))


def verify_wheel(wheel: Path, package: Path) -> dict:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        for source in (package / "src/velr").rglob("*.py"):
            if "_vendor" not in source.parts:
                name = source.relative_to(package / "src").as_posix()
                if name not in names or archive.read(name) != source.read_bytes():
                    raise ValueError(f"Wheel source is stale or missing: {name}")
        for document in DOCUMENTS:
            candidates = [name for name in names if name.endswith(f"/share/doc/velr/{document}")]
            if len(candidates) != 1 or archive.read(candidates[0]) != (package / document).read_bytes():
                raise ValueError(f"Wheel documentation is stale or missing: {document}")
        source_examples = discover_source_examples(package / "examples")
        expected_examples = {example["document"] for example in source_examples}
        packaged_examples = {"examples/" + name.split("/share/doc/velr/examples/", 1)[1]
                             for name in names if "/share/doc/velr/examples/" in name and name.endswith(".py")}
        if packaged_examples != expected_examples:
            raise ValueError(f"Wheel example inventory differs from reviewed programs: {sorted(packaged_examples ^ expected_examples)}")
        for example in source_examples:
            target = "/share/doc/velr/" + example["document"]
            candidates = [name for name in names if name.endswith(target)]
            if len(candidates) != 1 or archive.read(candidates[0]) != (package / example["document"]).read_bytes():
                raise ValueError(f"Wheel standalone example is stale or missing: {example['document']}")
        removed = {"jupyter", "jupyter_render", "nx", "sigma"}
        if any(name.startswith("velr/") and name.split("/")[1].split(".")[0] in removed for name in names):
            raise ValueError("Wheel contains a retired notebook or graph frontend module")
        native = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in names
                  if name.startswith("velr/_vendor/") and name.endswith((".so", ".dylib", ".dll"))}
        return {"path": str(wheel), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "native": native}


def check_examples(args) -> dict:
    manifest_text = MANIFEST.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    examples, details = inventory(PACKAGE, manifest)
    report = {"python": sys.version, "manifest_sha256": digest(manifest_text), "inventory": [{key: value for key, value in example.items() if key not in {"code", "doctest"}} for example in examples],
              "mirrored": details["mirrored"], "results": []}
    if args.inventory_only:
        return report
    if args.wheel is None:
        raise ValueError("Full verification requires --wheel PATH; source imports are never used")
    report["wheel"] = verify_wheel(args.wheel.resolve(strict=True), PACKAGE)
    wheels = [str(args.wheel.resolve())]
    if args.importer_wheel_dir is not None:
        importers = sorted(args.importer_wheel_dir.glob("velr_import-*.whl"))
        if len(importers) != 1:
            raise ValueError(f"Expected one standalone importer wheel in {args.importer_wheel_dir}, got {importers}")
        report["importer_wheel"] = str(importers[0].resolve(strict=True))
        wheels.append(report["importer_wheel"])
    consumer = Path(tempfile.mkdtemp(prefix="velr-python-doc-examples-"))
    report["consumer"] = str(consumer)
    passed = False
    try:
        site = consumer / "installed"
        install = run_process([*ISOLATED_PYTHON, "-m", "pip", "install", "--no-deps", "--no-compile",
                               "--target", str(site), *wheels], consumer, timeout=120)
        report["install"] = install
        if install["returncode"]:
            raise RuntimeError(f"Cannot install verification wheel: {install}")
        bootstrap = consumer / "bootstrap.py"
        bootstrap.write_text(BOOTSTRAP, encoding="utf-8")
        payload = consumer / "preflight.json"
        payload.write_text(json.dumps({"preflight": True, "importer": "importer_wheel" in report}), encoding="utf-8")
        preflight = run_process([*ISOLATED_PYTHON, str(bootstrap), str(site), str(payload), str(consumer / "preflight-output.json")], consumer)
        report["preflight"] = preflight
        if preflight["returncode"]:
            raise RuntimeError(f"Installed-wheel/required integrations preflight failed: {preflight}")
        for index, example in enumerate(examples):
            entry = details["entries"][example["id"]]
            if entry["kind"] != "run":
                continue
            if entry.get("requires_importer") and "importer_wheel" not in report:
                reason = "no standalone importer wheel specified"
                report["results"].append({"id": example["id"], "status": "skipped", "reason": reason})
                print(f"SKIPPED {example['id']}: {reason}", flush=True)
                continue
            directory = consumer / f"case-{index:03}"
            directory.mkdir()
            program = directory / "example.py"
            code = cypher_program(example, entry) if example["language"] == "cypher" else example["code"]
            compile(code, example["id"], "exec")
            program.write_text(code, encoding="utf-8")
            data = {"id": example["id"], "program": str(program)}
            if "doctest" in example:
                data["doctest"] = example["doctest"]
            payload = directory / "payload.json"
            payload.write_text(json.dumps(data), encoding="utf-8")
            output_file = directory / "output.json"
            result = run_process([*ISOLATED_PYTHON, str(bootstrap), str(site), str(payload), str(output_file)], directory, timeout=entry.get("timeout", 45))
            result["id"] = example["id"]
            try:
                if result["returncode"]:
                    raise AssertionError(f"Example exited {result['returncode']}")
                result["records"] = json.loads(output_file.read_text(encoding="utf-8"))
                verify_checks(entry.get("checks", []), result["records"], result["stdout"] + result["stderr"])
                result["status"] = "passed"
            except Exception as error:
                result["status"] = "failed"
                result["failure"] = str(error)
            report["results"].append(result)
            print(f"{result['status'].upper()} {example['id']} ({result['seconds']}s)", flush=True)
            if result["status"] == "failed":
                print(result.get("failure"), result["stdout"], result["stderr"], file=sys.stderr)
        failures = [result for result in report["results"] if result["status"] == "failed"]
        if failures:
            raise AssertionError(f"{len(failures)} documentation examples failed; evidence: {consumer}")
        passed = True
        return report
    finally:
        report["passed"] = passed
        if args.report:
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if passed and not args.keep_temp:
            shutil.rmtree(consumer)
        else:
            print(f"Verification evidence retained at {consumer}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-only", action="store_true", help="stdlib-only inventory, syntax, assertion, and freshness checks")
    parser.add_argument("--wheel", type=Path, help="compatible built wheel to install and test")
    parser.add_argument("--importer-wheel-dir", type=Path, default=os.environ.get("VELR_IMPORT_WHEEL_DIR") or None,
                        help="directory containing the standalone importer wheel for the CSV example")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--keep-temp", action="store_true")
    args = parser.parse_args()
    report = check_examples(args)
    passed = sum(result["status"] == "passed" for result in report["results"])
    print(f"Accounted for {len(report['inventory'])} examples/reference blocks and {len(report['mirrored'])} verified mirrors; executed {passed} programs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
