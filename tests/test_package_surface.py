"""The driver imports independently of notebook and graph-rendering frontends."""

import importlib.util
from pathlib import Path
import subprocess
import sys
import tomllib
import zipfile

DRIVER_ROOT = Path(__file__).resolve().parents[1]


def test_normal_import_and_query_need_no_frontend():
    code = r'''
import importlib.util
import sys

blocked = {"IPython", "ipykernel", "ipysigma", "ipywidgets", "networkx", "jupyter", "matplotlib", "plotly", "graphviz", "pyvis"}
class NoFrontendImports:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".", 1)[0] in blocked:
            raise AssertionError(f"Driver imported a frontend: {fullname}")

sys.meta_path.insert(0, NoFrontendImports())
import velr
from velr.driver import Velr, Table

with Velr.open() as db:
    db.run("CREATE (:Person {name:$name})", params={"name": "Alice"})
    assert db.to_records("MATCH (p:Person) RETURN p.name AS name") == [{"name": "Alice"}]

for module in ("jupyter", "jupyter_render", "nx", "sigma"):
    assert importlib.util.find_spec(f"velr.{module}") is None, module
assert not hasattr(Table, "_repr_html_")
assert not hasattr(Table, "_ipython_display_")
'''
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=30)


def test_distribution_extras_only_cover_driver_data_integrations():
    project = tomllib.loads((DRIVER_ROOT / "pyproject.toml").read_text())["project"]
    extras = project["optional-dependencies"]
    assert not {"nx", "jupyter", "notebook", "visualization"}.intersection(extras)
    assert {"pandas", "polars"}.issubset(extras)
    requirements = project["dependencies"] + [value for values in extras.values() for value in values]
    assert not any(name.lower().startswith(("networkx", "ipython", "ipysigma", "jupyter", "ipywidgets")) for name in requirements)


def test_wheel_guard_rejects_stale_graph_modules_and_dependencies(tmp_path):
    script = DRIVER_ROOT / "scripts/check_package.py"
    spec = importlib.util.spec_from_file_location("driver_package_check", script)
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    wheel = tmp_path / "velr-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name in ("__init__", "driver", "_ffi"):
            archive.writestr(f"velr/{name}.py", "")
        archive.writestr("velr/sigma.py", "")
        archive.writestr("velr-0.0.0.dist-info/METADATA", "Metadata-Version: 2.4\nName: velr\nVersion: 0.0.0\nRequires-Dist: networkx>=3.2; extra == 'all'\nProvides-Extra: nx\n")
    errors = checker.check_wheel(wheel)
    assert any("velr/sigma.py" in error for error in errors)
    assert any("networkx" in error for error in errors)
    assert any("extra packaged: nx" in error for error in errors)


def test_wheel_guard_requires_the_guides_examples_and_linked_licenses(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "driver_package_check", DRIVER_ROOT / "scripts/check_package.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    files = {f"velr/{name}.py": "" for name in (
        "__init__", "driver", "_ffi", "guide", "query_language")}
    files["velr/_vendor/libvelrc.dylib"] = "runtime fixture"
    files["velr-0.0.0.dist-info/METADATA"] = "Metadata-Version: 2.4\nName: velr\nVersion: 0.0.0\n"
    prefix = "velr-0.0.0.data/data/share/doc/velr/"
    for name in ("README.md", "GUIDE.md", "QUERY_LANGUAGE.md", "API_REFERENCE.md",
                 "LICENSE", "LICENSE.runtime", "examples/basic_query.py", "examples/txn_savepoints.py"):
        files[prefix + name] = "documentation fixture"
    wheel = tmp_path / "velr-0.0.0-py3-none-any.whl"

    def write_wheel():
        with zipfile.ZipFile(wheel, "w") as archive:
            for name, content in files.items():
                archive.writestr(name, content)

    write_wheel()
    assert checker.check_wheel(wheel) == []
    del files[prefix + "GUIDE.md"]
    del files[prefix + "examples/basic_query.py"]
    del files[prefix + "LICENSE.runtime"]
    write_wheel()
    errors = checker.check_wheel(wheel)
    assert any("Missing packaged documentation: GUIDE.md" in error for error in errors)
    assert any("Missing packaged documentation: examples/basic_query.py" in error for error in errors)
    assert any("Missing packaged documentation: LICENSE.runtime" in error for error in errors)
