#!/usr/bin/env python3
"""Validate the public Python bindings and binary-only core runtime wheel."""

from __future__ import annotations

import argparse
from email.parser import Parser
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import zipfile

DRIVER_FILES = frozenset({
    "velr/__init__.py", "velr/_ffi.py", "velr/_vendor/__init__.py", "velr/driver.py",
    "velr/guide.py", "velr/query_language.py",
})
OPTIONAL_DRIVER_FILES = frozenset({"velr/extensions.py", "velr/py.typed"})
DOCUMENTATION_FILES = (
    "README.md", "GUIDE.md", "QUERY_LANGUAGE.md", "API_REFERENCE.md", "LICENSE",
    "LICENSE.runtime", "examples/basic_query.py", "examples/txn_savepoints.py",
)
FRONTEND_DEPENDENCIES = {
    "ipython", "ipykernel", "ipysigma", "ipywidgets", "networkx", "notebook",
    "matplotlib", "plotly", "graphviz", "pyvis", "bokeh",
}


def runtime_for_wheel(path: Path) -> tuple[str, str] | None:
    platforms = set()
    for tag in path.stem.rsplit("-", 1)[-1].split("."):
        if linux := re.fullmatch(r"(?:linux|manylinux(?:_\d+_\d+|1|2010|2014))_(x86_64|aarch64)", tag):
            platforms.add(("libvelrc.so", linux[1]))
        elif mac := re.fullmatch(r"macosx_\d+_\d+_(arm64|x86_64|universal2)", tag):
            platforms.add(("libvelrc.dylib", mac[1]))
        elif tag == "win_amd64":
            platforms.add(("velrc.dll", "x86_64"))
        else:
            return None
    return platforms.pop() if len(platforms) == 1 else None


def native_header_matches(data: bytes, library: str, arch: str, *, historical: bool) -> bool:
    if library == "libvelrc.so":
        machine = 62 if arch == "x86_64" else 183
        return (data.startswith(b"\x7fELF\x02\x01")
                and data[16:20] == struct.pack("<HH", 3, machine))
    if library == "velrc.dll":
        if not data.startswith(b"MZ") or len(data) < 64:
            return False
        offset = struct.unpack_from("<I", data, 60)[0]
        return (data[offset:offset + 6] == b"PE\0\0\x64\x86"
                and bool(int.from_bytes(data[offset + 22:offset + 24], "little") & 0x2000))

    cpus = {"x86_64": 0x1000007, "arm64": 0x100000C}

    def thin_matches(header: bytes, cpu: int) -> bool:
        return (header.startswith(struct.pack("<II", 0xFEEDFACF, cpu))
                and header[12:16] == struct.pack("<I", 6))

    if arch != "universal2":
        return thin_matches(data, cpus[arch])
    # Historical universal2 wheels could carry a thin Mach-O. Current
    # publication compares against the release source and requires both slices.
    if historical and any(thin_matches(data, cpu) for cpu in cpus.values()):
        return True
    if data[:8] != b"\xca\xfe\xba\xbe\0\0\0\x02" or len(data) < 48:
        return False
    slices = set()
    for header in (8, 28):
        cpu, _, offset, size, _ = struct.unpack_from(">5I", data, header)
        if offset < 48 or size < 16 or offset + size > len(data) or not thin_matches(data[offset:offset + 16], cpu):
            return False
        slices.add(cpu)
    return slices == set(cpus.values())


def check_wheel(path: Path, *, source: Path | None = None) -> list[str]:
    errors = []
    with zipfile.ZipFile(path) as wheel:
        names = wheel.namelist()
        metadata_files = [name for name in names if name.endswith(".dist-info/METADATA")]
        if len(metadata_files) != 1:
            return ["Expected exactly one wheel METADATA file"]
        metadata = Parser().parsestr(wheel.read(metadata_files[0]).decode("utf-8"))
        if metadata["Name"] != "velr":
            errors.append("Expected the embedded velr driver package")
        version = metadata["Version"]
        if not version or not path.name.startswith(f"velr-{version}-"):
            errors.append("Wheel filename disagrees with METADATA version")
        prefix = f"velr-{version}"
        info = f"{prefix}.dist-info"
        docs = f"{prefix}.data/data/share/doc/velr"
        required = set(DRIVER_FILES) | {f"{info}/{name}" for name in ("METADATA", "WHEEL", "RECORD")}
        optional = set(OPTIONAL_DRIVER_FILES) | {f"{info}/top_level.txt"}
        source_files = {name: f"src/{name}" for name in DRIVER_FILES}
        if source is not None:
            for name in OPTIONAL_DRIVER_FILES:
                source_path = f"src/{name}"
                if (source / source_path).is_file():
                    required.add(name)
                    source_files[name] = source_path
        for name in DOCUMENTATION_FILES:
            member = f"{docs}/{name}"
            if member not in names:
                errors.append(f"Missing packaged documentation: {name}")
            required.add(member)
            source_files[member] = name
        for license_name in ("LICENSE", "LICENSE.runtime"):
            licensed = {f"{info}/{license_name}", f"{info}/licenses/{license_name}"}
            if not licensed.intersection(names):
                errors.append(f"Missing wheel distribution license: {license_name}")
            optional.update(licensed)
            source_files.update({name: license_name for name in licensed if name in names})

        platform = runtime_for_wheel(path)
        if platform is None:
            errors.append("Expected one supported wheel platform")
        else:
            library, arch = platform
            member = f"velr/_vendor/{library}"
            required.add(member)
            if member not in names:
                errors.append(f"Missing bundled Velr runtime: {member}")
            elif not native_header_matches(wheel.read(member), library, arch, historical=source is None):
                errors.append(f"Bundled Velr runtime does not match the wheel platform: {member}")

        allowed = required | optional
        directories = {f"{parent}/" for name in allowed for parent in PurePosixPath(name).parents if parent.parts}
        if len(set(names)) != len(names):
            errors.append("Duplicate wheel member names")
        for member in wheel.infolist():
            name = member.filename
            mode = stat.S_IFMT(member.external_attr >> 16)
            if member.is_dir():
                if name not in directories or member.file_size or mode not in {0, stat.S_IFDIR}:
                    errors.append(f"Unexpected wheel directory: {name}")
            elif name not in allowed or mode not in {0, stat.S_IFREG}:
                errors.append(f"Unexpected wheel member: {name}")
        missing = required - set(names)
        if missing:
            errors.append(f"Missing required wheel files: {', '.join(sorted(missing))}")

        if source is not None:
            for member, expected in sorted(source_files.items()):
                if member not in names:
                    continue
                source_file = source / expected
                if (not source_file.is_file() or source_file.is_symlink()
                        or wheel.read(member).replace(b"\r\n", b"\n") != source_file.read_bytes().replace(b"\r\n", b"\n")):
                    errors.append(f"Wheel member differs from the reviewed driver source: {member}")
            if "velr/extensions.py" in names and "velr/extensions.py" not in source_files:
                errors.append("Wheel extensions module is absent from the reviewed driver source")
        if "velr/py.typed" in names and wheel.read("velr/py.typed") not in {b"", b"partial\n", b"partial\r\n"}:
            errors.append("Unexpected content in velr/py.typed")
        top_level = f"{info}/top_level.txt"
        if top_level in names and wheel.read(top_level).replace(b"\r\n", b"\n") != b"velr\n":
            errors.append("Unexpected top-level modules in wheel metadata")

        for requirement in metadata.get_all("Requires-Dist", []):
            dependency = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", requirement)
            name = dependency[0].lower().replace("_", "-") if dependency else ""
            if name in FRONTEND_DEPENDENCIES or name.startswith("jupyter"):
                errors.append(f"Notebook/graph dependency packaged: {requirement}")
        for extra in metadata.get_all("Provides-Extra", []):
            if extra in {"nx", "jupyter", "notebook", "visualization"}:
                errors.append(f"Removed notebook/graph extra packaged: {extra}")
    with path.open("rb") as stream:
        signature = stream.read(4)
        stream.seek(-22, 2)
        trailer = stream.read()
    if signature != b"PK\x03\x04" or not trailer.startswith(b"PK\x05\x06") or not trailer.endswith(b"\0\0"):
        errors.append("Wheel contains an unexpected header, ZIP comment or trailing data")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--check-source", action="store_true", help="Compare reviewed wrapper and documentation files with this checkout")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] if args.check_source else None
    errors = check_wheel(args.wheel, source=source)
    if errors:
        print("\n".join(errors))
        return 1
    print(f"Checked Python driver package surface: {args.wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
