import json
import shutil
import subprocess
import sys
from pathlib import Path

from tests.contracts.swarm_v2 import build_fixtures, consumer

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "docs" / "swarm-v2" / "schemas"
HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
COMPAT = json.loads((SCHEMAS / "compatibility.json").read_text())
CORE = ["execution_id", "task_id", "task_generation"]


def _consume(tmp_path: Path) -> tuple[int, dict]:
    schemas = tmp_path / "schemas"
    fixtures = tmp_path / "fixtures"
    shutil.copytree(SCHEMAS, schemas)
    shutil.copytree(FIXTURES, fixtures)
    shutil.copy(HERE / "consumer.py", tmp_path / "consumer.py")
    proc = subprocess.run(
        [sys.executable, "-I", "consumer.py", str(schemas), str(fixtures)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_a_consumer_validates_every_shared_fixture_without_the_producer_package(tmp_path):
    code, report = _consume(tmp_path)
    assert report["producer_modules"] == []
    assert [r for r in report["results"] if r["expect"] != r["got"]] == []
    assert len(report["results"]) == 46
    assert code == 0


def test_a_second_independent_consumer_run_reaches_the_same_verdicts(tmp_path):
    first = _consume(tmp_path / "one")
    second = _consume(tmp_path / "two")
    assert first == second


def test_a_consumer_fails_when_a_fixture_breaks_its_expectation(tmp_path):
    shutil.copytree(FIXTURES, tmp_path / "broken")
    doc = json.loads((tmp_path / "broken" / "launch" / "current.json").read_text())
    doc["schema_version"] = "3.0"
    (tmp_path / "broken" / "launch" / "current.json").write_text(json.dumps(doc))
    results = consumer.verdicts(SCHEMAS, tmp_path / "broken")
    assert {"file": "launch/current.json", "expect": "valid", "got": "invalid"} in results


def test_every_family_is_published_for_every_contract():
    index = json.loads((FIXTURES / "index.json").read_text())["fixtures"]
    families = {}
    for entry in index:
        families.setdefault(entry["contract"], set()).add(entry["family"])
    for contract, spec in COMPAT["contracts"].items():
        expected = {"current", "previous-minor", "future-major", "newer-minor"}
        if spec["authority"]:
            expected |= {"missing-authority", "previous-minor-missing-authority"}
        assert families[contract] == expected


def test_committed_fixtures_match_their_generator():
    for rel, text in build_fixtures.build().items():
        assert (FIXTURES / rel).read_text() == text, rel


def test_the_generator_writes_into_the_named_folder(tmp_path):
    shutil.copytree(FIXTURES / "heartbeat", tmp_path / "heartbeat")
    (tmp_path / "heartbeat" / "future-major.json").unlink()
    assert build_fixtures.main([str(tmp_path)]) == 0
    assert (tmp_path / "heartbeat" / "future-major.json").read_text() == (
        FIXTURES / "heartbeat" / "future-major.json"
    ).read_text()
    index = json.loads((tmp_path / "index.json").read_text())["fixtures"]
    assert [e["family"] for e in index] == [
        "current",
        "previous-minor",
        "future-major",
        "missing-authority",
        "previous-minor-missing-authority",
        "newer-minor",
    ]


def test_manifest_authority_lists_match_each_schema():
    for contract, spec in COMPAT["contracts"].items():
        schema = json.loads((SCHEMAS / spec["schema"]).read_text())
        authority = schema["properties"].get("authority", {})
        assert authority.get("required", []) == spec["authority"]
        if spec["authority"]:
            assert spec["authority"][:3] == CORE
            assert authority["additionalProperties"] is False


def test_manifest_names_one_owner_and_supported_minors_per_contract():
    for spec in COMPAT["contracts"].values():
        assert spec["owner"] in {"agentihooks", "agentibrain-kernel"}
        assert spec["major"] == 2
        assert spec["oldest_minor"] <= spec["write_minor"] <= spec["current_minor"]
        assert f"{spec['major']}.{spec['current_minor']}" in COMPAT["minors"]
    owners = {name: spec["owner"] for name, spec in COMPAT["contracts"].items()}
    assert owners["context_pack"] == owners["receipt"] == "agentibrain-kernel"


def test_schemas_reference_only_identifiers_in_their_own_folder():
    ids = set()
    refs = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "$ref":
                    refs.append(value)
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for path in SCHEMAS.glob("*.json"):
        doc = json.loads(path.read_text())
        if "$id" in doc:
            ids.add(doc["$id"])
        walk(doc)
    assert refs
    for ref in refs:
        base = ref.split("#")[0]
        assert base == "" or base in ids, ref


def test_authority_names_are_reserved_outside_the_authority_object():
    common = json.loads((SCHEMAS / "common.json").read_text())
    reserved = set(common["$defs"]["authority_names"]["enum"])
    for spec in COMPAT["contracts"].values():
        schema = json.loads((SCHEMAS / spec["schema"]).read_text())
        assert set(schema["properties"].get("authority", {}).get("properties", {})) <= reserved
        assert not set(schema["properties"]) & reserved
