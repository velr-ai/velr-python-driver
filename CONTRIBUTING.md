# Contributing to the Python driver

This repository contains the embedded Python binding, its documentation,
examples, and tests. The native Velr runtime is distributed in the published
`velr` wheels under [LICENSE.runtime](LICENSE.runtime). This repository contains
no runtime binaries or native runtime source.

Use Python 3.12 or later. To work on a published release, install its matching
wheel in a virtual environment, then copy the platform runtime from that wheel
into the ignored `src/velr/_vendor` directory before installing this checkout.
Run these commands from the repository root:

```sh
python -m venv .venv
```

Activate the environment (`source .venv/bin/activate` on macOS/Linux or
`.venv\Scripts\Activate.ps1` in PowerShell), then run:

```sh
python -m pip install --only-binary=:all: "velr==0.2.67"
python -c "from importlib.metadata import distribution; from pathlib import Path; from shutil import copyfile; p = Path(distribution('velr').locate_file('velr/_vendor')); names = ('libvelrc.dylib', 'libvelrc.so', 'velrc.dll'); files = [p / n for n in names if (p / n).is_file()]; assert len(files) == 1, 'Expected one runtime in the installed wheel'; copyfile(files[0], Path('src/velr/_vendor') / files[0].name)"
python -m pip install -e ".[test]"
python -m pytest tests
```

If this source version has no published wheel yet, native execution requires a
compatible runtime supplied separately. Importing `velr`, `velr.guide`, and
`velr.query_language` does not load the runtime; accessing driver classes does.
Keep runtime files local: they are ignored and must not be committed.

Read [README.md](README.md) for application usage and [API_REFERENCE.md](API_REFERENCE.md)
for the public API. Changes to public APIs need corresponding docstrings and
usage documentation. The Python binding is covered by [LICENSE](LICENSE).
