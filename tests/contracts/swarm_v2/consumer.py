import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource


def registry(schemas: Path) -> Registry:
    resources = []
    for path in sorted(schemas.glob("*.json")):
        doc = json.loads(path.read_text())
        if "$id" in doc:
            resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


def verdicts(schemas: Path, fixtures: Path) -> list[dict]:
    reg = registry(schemas)
    compat = json.loads((schemas / "compatibility.json").read_text())
    out = []
    for entry in json.loads((fixtures / "index.json").read_text())["fixtures"]:
        schema = json.loads((schemas / compat["contracts"][entry["contract"]]["schema"]).read_text())
        doc = json.loads((fixtures / entry["file"]).read_text())
        valid = Draft202012Validator(schema, registry=reg).is_valid(doc)
        out.append({"file": entry["file"], "expect": entry["expect"], "got": "valid" if valid else "invalid"})
    return out


def main(argv: list[str]) -> int:
    results = verdicts(Path(argv[0]), Path(argv[1]))
    leaked = sorted(name for name in sys.modules if name.split(".")[0] in {"scripts", "hooks"})
    print(json.dumps({"results": results, "producer_modules": leaked}))
    return 0 if not leaked and all(r["expect"] == r["got"] for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
