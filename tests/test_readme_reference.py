"""Execute the actual introductory snippets and shared query-reference examples."""

from pathlib import Path
import re

import pytest

from velr.driver import Velr, VelrError

DRIVER_ROOT = Path(__file__).resolve().parents[1]
REFERENCE = DRIVER_ROOT / "QUERY_LANGUAGE.md"
QUERIES = re.findall(r"```cypher\n(.*?)\n```", REFERENCE.read_text(encoding="utf-8"), re.S)
GUIDE = (DRIVER_ROOT / "GUIDE.md").read_text(encoding="utf-8")
README = (DRIVER_ROOT / "README.md").read_text(encoding="utf-8")
README_EXAMPLES = []
for match in re.finditer(r"```python\n(.*?)\n```", README, re.S):
    headings = re.findall(r"^#{2,3} (.*)$", README[:match.start()], re.M)
    README_EXAMPLES.append((headings[-1], match.group(1)))
GUIDE_EXAMPLES = []
for match in re.finditer(r"```python\n(.*?)\n```", GUIDE, re.S):
    headings = re.findall(r"^## (.*)$", GUIDE[:match.start()], re.M)
    section = headings[-1] if headings else "Introduction"
    GUIDE_EXAMPLES.append((section, match.group(1)))


@pytest.mark.parametrize("section,code", README_EXAMPLES)
def test_readme_python_examples(section, code, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    exec(compile(code, f"README.md: {section}", "exec"), {"__name__": "__main__"})
    if section == "Quick start":
        assert capsys.readouterr().out == "[{'name': 'Alice', 'age': 42}]\n"


@pytest.mark.parametrize("index,cypher", list(enumerate(QUERIES)))
def test_query_reference_examples(index, cypher, tmp_path):
    with Velr.open(str(tmp_path / f"reference-{index}.db")) as db:
        with db.exec(cypher, params={"people": [{"name": "Alice", "age": 42}]}) as stream:
            results = []
            while (table := stream.next_table()) is not None:
                with table:
                    results.append(table.to_records())
        last = results[-1] if results else []
        if "AS oddSquares" in cypher:
            assert last == [{"oddSquares": [1, 9, 25], "total": 6}]
        elif "active: true" in cypher:
            assert last == [{"person": {"name": "Alice", "age": 42, "active": True}}]
        elif "AS anyNodes" in cypher:
            assert last == [{"anyNodes": False, "nodeCount": 0}]
        elif "RETURN x, doubled" in cypher:
            assert last == [{"x": 1, "doubled": 2}, {"x": 2, "doubled": 4}]
        elif "AS enabled" in cypher:
            assert last == [{"number": 1.5, "enabled": True, "upper": "HELLO", "replaced": "heyo", "prefix": "he", "suffix": "lo"}]
        elif "AS median" in cypher:
            assert last == [{"count": 3, "total": 6, "average": 2.0, "median": 2, "floor": 1.0, "rounded": 1.3}]
        elif "AS seconds" in cypher:
            assert last[0]["seconds"] == 1
        elif "queryNodes(" in cypher:
            assert len(last) == 1
            assert isinstance(last[0]["score"], (int, float))
        elif "CREATE (:Person" in cypher:
            assert db.to_records("MATCH (p:Person) RETURN p.name AS name") == [{"name": "Alice"}]


def test_documented_metadata_and_owned_values_lifetime():
    with Velr.open() as db:
        table = db.exec_one("RETURN 'Alice' AS name")
        names = table.column_names()
        assert table.column_count() == 1
        records = table.to_records()
        table.close()
        with pytest.raises(VelrError):
            table.column_names()
        with pytest.raises(VelrError):
            table.column_count()
    assert names == ["name"]
    assert records == [{"name": "Alice"}]


@pytest.mark.parametrize("section,code", GUIDE_EXAMPLES)
def test_core_guide_examples(section, code, tmp_path, monkeypatch):
    """Run each standalone guide program without an injected database or namespace."""
    monkeypatch.chdir(tmp_path)
    exec(compile(code, f"GUIDE.md: {section}", "exec"), {"__name__": "__main__"})
