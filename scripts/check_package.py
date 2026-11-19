#!/usr/bin/env python3
"""Reject notebook and graph-visualization additions in built Python driver wheels."""

from __future__ import annotations

import argparse
from email.parser import Parser
from pathlib import Path, PurePosixPath
import re
import zipfile

REMOVED_MODULES = {"jupyter", "jupyter_render", "nx", "sigma"}
FRONTEND_DEPENDENCIES = {
    "ipython", "ipykernel", "ipysigma", "ipywidgets", "networkx", "notebook",
    "matplotlib", "plotly", "graphviz", "pyvis", "bokeh",
}


def check_wheel(path: Path) -> list[str]:
    errors = []
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        for name in names:
            parts = PurePosixPath(name).parts
            if len(parts) >= 2 and parts[0] == "velr":
                if parts[1].split(".", 1)[0] in REMOVED_MODULES:
                    errors.append(f"Removed notebook/graph module packaged: {name}")
        metadata_files = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            return errors + ["Expected exactly one wheel METADATA file"]
        metadata = Parser().parsestr(wheel.read(metadata_files[0]).decode("utf-8"))
        if metadata["Name"] != "velr":
            errors.append("Expected the embedded velr driver package")
        for requirement in metadata.get_all("Requires-Dist", []):
            dependency = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
            name = dependency[0].lower().replace("_", "-") if dependency else ""
            if name in FRONTEND_DEPENDENCIES or name.startswith("jupyter"):
                errors.append(f"Notebook/graph dependency packaged: {requirement}")
        for extra in metadata.get_all("Provides-Extra", []):
            if extra in {"nx", "jupyter", "notebook", "visualization"}:
                errors.append(f"Removed notebook/graph extra packaged: {extra}")
        for required in ("velr/__init__.py", "velr/driver.py", "velr/_ffi.py",
                         "velr/guide.py", "velr/query_language.py"):
            if required not in names:
                errors.append(f"Missing driver module: {required}")
        for required in ("README.md", "GUIDE.md", "QUERY_LANGUAGE.md", "API_REFERENCE.md",
                         "LICENSE", "LICENSE.runtime", "examples/basic_query.py",
                         "examples/txn_savepoints.py"):
            if not any(name.endswith(f"/share/doc/velr/{required}") for name in names):
                errors.append(f"Missing packaged documentation: {required}")
        if not any(f"velr/_vendor/{filename}" in names
                   for filename in ("libvelrc.dylib", "libvelrc.so", "velrc.dll")):
            errors.append("Missing bundled Velr runtime")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    errors = check_wheel(args.wheel)
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Checked Python driver package surface: {args.wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
