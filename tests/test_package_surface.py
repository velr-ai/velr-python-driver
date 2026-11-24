"""The driver imports independently of notebook and graph-rendering frontends."""

import importlib.util
from pathlib import Path
import struct
import subprocess
import sys
import tomllib
import zipfile

DRIVER_ROOT = Path(__file__).resolve().parents[1]
WHEEL_PREFIX = "velr-0.0.0"
DOCS_PREFIX = f"{WHEEL_PREFIX}.data/data/share/doc/velr/"
DIST_INFO = f"{WHEEL_PREFIX}.dist-info/"


def wheel_checker():
    spec = importlib.util.spec_from_file_location(
        "driver_package_check", DRIVER_ROOT / "scripts/check_package.py")
    checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(checker)
    return checker


def driver_wheel_members(checker, *, source=None):
    bindings = {wheel: f"src/{wheel}" for wheel in checker.DRIVER_FILES}
    if source is not None:
        bindings.update({wheel: f"src/{wheel}" for wheel in checker.OPTIONAL_DRIVER_FILES
                         if (source / "src" / wheel).is_file()})
    docs = {DOCS_PREFIX + name: name for name in checker.DOCUMENTATION_FILES}
    licenses = {DIST_INFO + "licenses/" + name: name for name in ("LICENSE", "LICENSE.runtime")}
    files = {wheel: (source / path).read_bytes() if source else b"reviewed public fixture"
             for wheel, path in {**bindings, **docs, **licenses}.items()}
    files[DIST_INFO + "METADATA"] = b"Metadata-Version: 2.4\nName: velr\nVersion: 0.0.0\n"
    files[DIST_INFO + "WHEEL"] = b"Wheel-Version: 1.0\nRoot-Is-Purelib: false\n"
    files[DIST_INFO + "RECORD"] = b""
    return files


def write_wheel(path, files):
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)


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
    checker = wheel_checker()
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
    checker = wheel_checker()
    files = driver_wheel_members(checker)
    files["velr/_vendor/libvelrc.dylib"] = struct.pack("<IIII", 0xFEEDFACF, 0x100000C, 0, 6)
    wheel = tmp_path / f"{WHEEL_PREFIX}-cp312-cp312-macosx_11_0_arm64.whl"

    write_wheel(wheel, files)
    assert checker.check_wheel(wheel) == []
    del files[DOCS_PREFIX + "GUIDE.md"]
    del files[DOCS_PREFIX + "examples/basic_query.py"]
    del files[DOCS_PREFIX + "LICENSE.runtime"]
    write_wheel(wheel, files)
    errors = checker.check_wheel(wheel)
    assert any("Missing packaged documentation: GUIDE.md" in error for error in errors)
    assert any("Missing packaged documentation: examples/basic_query.py" in error for error in errors)
    assert any("Missing packaged documentation: LICENSE.runtime" in error for error in errors)


def test_wheel_guard_rejects_implementation_source_and_an_extra_runtime(tmp_path):
    checker = wheel_checker()
    files = driver_wheel_members(checker)
    runtime = "velr/_vendor/libvelrc.so"
    files[runtime] = b"\x7fELF\x02\x01" + bytes(10) + struct.pack("<HH", 3, 62)
    files["velr/"] = files["velr/_vendor/"] = b""
    wheel = tmp_path / f"{WHEEL_PREFIX}-cp312-cp312-manylinux_2_35_x86_64.whl"
    write_wheel(wheel, files)
    assert checker.check_wheel(wheel) == []

    foreign_files = (
        "rust/velr-core/src/lib.rs", "velr_gds/algorithm.rs", "velr_import/_native.py",
        "velr/_vendor/libvelr_gds.so", "velr/_vendor/velrc.dll", "velr/_vendor/private/",
    )
    write_wheel(wheel, {**files, **dict.fromkeys(foreign_files, b"implementation")})
    errors = checker.check_wheel(wheel)
    assert all(any(name in error for error in errors) for name in foreign_files)

    files[runtime] = b"fn confidential() {}"
    write_wheel(wheel, files)
    assert any("does not match the wheel platform" in error for error in checker.check_wheel(wheel))


def test_release_wheel_guard_checks_reviewed_files_across_line_endings(tmp_path):
    checker = wheel_checker()
    files = driver_wheel_members(checker, source=DRIVER_ROOT)
    files["velr/_vendor/velrc.dll"] = (
        b"MZ" + bytes(58) + struct.pack("<I", 64) + b"PE\0\0\x64\x86" + bytes(16) + b"\x00\x20"
    )
    readme = DOCS_PREFIX + "README.md"
    # Exercise different line endings even when Git checks out the README with CRLF.
    if b"\r\n" in files[readme]:
        files[readme] = files[readme].replace(b"\r\n", b"\n")
    else:
        files[readme] = files[readme].replace(b"\n", b"\r\n")
    wheel = tmp_path / f"{WHEEL_PREFIX}-cp312-cp312-win_amd64.whl"
    write_wheel(wheel, files)
    assert checker.check_wheel(wheel, source=DRIVER_ROOT) == []

    overwritten = DOCS_PREFIX + "examples/basic_query.py"
    files[overwritten] = b"unreviewed implementation with an approved filename"
    write_wheel(wheel, files)
    assert any(overwritten in error for error in checker.check_wheel(wheel, source=DRIVER_ROOT))
