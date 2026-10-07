import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

SCHEMA = "swarm-v2-evidence-index/1"
PLAN = "Swarm-v2.md"
INDEX = "docs/swarm-v2/evidence-index.json"
MARKDOWN = "docs/swarm-v2/evidence-index.md"
CLASSES = ("transcript_durability", "scope_isolation", "execution_safety", "knowledge_integrity")
CLAIMS = ("open", "complete")
CASES = (("A", "positive case result"), ("B", "negative case result"), ("C", "recovery proof"))
PROVES = frozenset({"test", "live_canary"})
NEEDS_COMMIT = frozenset({"test", "pull_request", "live_canary", "migration"})
NEEDS_CASE = frozenset({"test", "live_canary", "experiment"})
NEEDS_OUTCOME = frozenset({"test", "live_canary", "experiment", "migration"})
KINDS = NEEDS_COMMIT | NEEDS_CASE | {"screenshot"}
REF = "ref must be an https link or a repository file with its sha256"
PACKAGE_RE = re.compile(r"^#### (SV2-[A-Z]+-\d\d): ", re.M)
PACKAGE_ID = re.compile(r"SV2-[A-Z]+-\d\d")
INVARIANT_RE = re.compile(r"^`(INV-[A-Z]\d\d)`:", re.M)
CASE_RE = re.compile(r"^- (?:Positive|Negative|Recovery) case: \[(T-SV2-[A-Z]+-\d\d-([ABC]))\]", re.M)
URL_RE = re.compile(r"https://\S+")
SHA_RE = re.compile(r"[0-9a-f]{40}")


class EvidenceError(ValueError):
    pass


def _field(section: str, name: str) -> str:
    match = re.search(rf"^- {name}: (.*)$", section, re.M)
    return match.group(1) if match else ""


def _package(section: str) -> dict:
    return {
        "repository": _field(section, "Repository").strip("`."),
        "gate": _field(section, "Integration gate").split("`")[1],
        "dependencies": PACKAGE_ID.findall(_field(section, "Dependencies")),
        "cases": {case: test for test, case in CASE_RE.findall(section)},
    }


def load_plan(path: Path | str) -> dict:
    text = Path(path).read_text()
    heads = list(PACKAGE_RE.finditer(text))
    ends = [h.start() for h in heads[1:]] + [len(text)]
    packages = {}
    for head, end in zip(heads, ends):
        section = text[head.end() : end].split("\n#### ", 1)[0]
        packages[head.group(1)] = _package(section)
    return {"packages": packages, "invariants": INVARIANT_RE.findall(text)}


def gates(plan: dict) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for pid, spec in plan["packages"].items():
        grouped.setdefault(spec["gate"], []).append(pid)
    return {gate: grouped[gate] for gate in sorted(grouped, key=lambda g: int(g[1:]))}


def load_index(path: Path | str) -> dict:
    data = json.loads(Path(path).read_text())
    if data.get("schema") != SCHEMA:
        raise EvidenceError(f"{path} is not a {SCHEMA} document")
    return data


def _ref_error(item: dict, root: Path) -> str:
    ref = item.get("ref", "")
    if URL_RE.fullmatch(ref):
        return ""
    parts = PurePosixPath(ref).parts
    if not ref or ref.startswith("/") or ".." in parts or "sha256" not in item:
        return REF
    file = root / ref
    if not file.is_file():
        return f"{ref} is missing"
    if hashlib.sha256(file.read_bytes()).hexdigest() != item["sha256"]:
        return f"{ref} does not match its recorded sha256"
    return ""


def _item_errors(item: dict, root: Path) -> list[str]:
    kind = item.get("kind")
    if kind not in KINDS:
        return [f"unknown kind {kind!r}"]
    errors = [_ref_error(item, root)]
    if kind in NEEDS_COMMIT and not SHA_RE.fullmatch(item.get("commit", "")):
        errors.append("commit must be a full 40 character sha")
    if (kind in NEEDS_CASE or "case" in item) and item.get("case") not in dict(CASES):
        errors.append("case must be one of A, B, C")
    if kind in NEEDS_OUTCOME and item.get("outcome") not in ("passed", "failed"):
        errors.append("outcome must be passed or failed")
    return [e for e in errors if e]


def _requirement_errors(index: dict, plan: dict) -> list[str]:
    errors = []
    mapped = set()
    for req in index["requirements"]:
        if req["class"] not in CLASSES:
            errors.append(f"{req['id']}: unknown class {req['class']!r}")
        errors += [f"{req['id']}: names unknown package {p}" for p in req["packages"] if p not in plan["packages"]]
        if req["id"] not in plan["invariants"]:
            errors.append(f"{req['id']} is not an invariant of the plan")
        if req["packages"]:
            mapped.add(req["id"])
    return errors + [f"{inv} is not mapped to any package" for inv in plan["invariants"] if inv not in mapped]


def _package_errors(index: dict, plan: dict, root: Path) -> list[str]:
    errors = []
    ids = []
    for pid, entry in index["packages"].items():
        if pid not in plan["packages"]:
            errors.append(f"{pid} is not a package of the plan")
            continue
        if entry["claim"] not in CLAIMS:
            errors.append(f"{pid}: unknown claim {entry['claim']!r}")
        for item in entry["evidence"]:
            if "id" not in item:
                errors.append(f"{pid}: evidence needs an id")
                continue
            ids.append(item["id"])
            errors += [f"{item['id']}: {e}" for e in _item_errors(item, root)]
    return errors + [f"{i} is recorded more than once" for i in dict.fromkeys(i for i in ids if ids.count(i) > 1)]


def validate(index: dict, plan: dict, root: Path | str) -> list[str]:
    return _requirement_errors(index, plan) + _package_errors(index, plan, Path(root))


def _classes(index: dict, pid: str) -> list[str]:
    found = {r["class"] for r in index["requirements"] if pid in r["packages"]}
    return [c.replace("_", " ") for c in CLASSES if c in found]


def _proves(item: dict, case: str) -> bool:
    return item.get("kind") in PROVES and item.get("case") == case and item.get("outcome") == "passed"


def _pull_request(item: dict, repository: str) -> bool:
    return item.get("kind") == "pull_request" and bool(
        re.fullmatch(rf"https://github\.com/[\w.-]+/{re.escape(repository)}/pull/\d+", item.get("ref", ""))
    )


def _screenshot_note(evidence: list[dict], case: str, classes: list[str]) -> str:
    if not any(e.get("kind") == "screenshot" and e.get("case", case) == case for e in evidence):
        return ""
    scope = f"{' and '.join(classes)} acceptance" if classes else "acceptance"
    return f"; a screenshot cannot satisfy {scope}"


def _missing(index: dict, plan: dict, pid: str) -> list[str]:
    spec, evidence = plan["packages"][pid], index["packages"][pid]["evidence"]
    reasons = []
    if not any(_pull_request(e, spec["repository"]) for e in evidence):
        reasons.append("no pull request with a tested commit")
    classes = _classes(index, pid)
    for case, label in CASES:
        if not any(_proves(e, case) for e in evidence):
            reasons.append(f"{spec['cases'][case]}: missing {label}{_screenshot_note(evidence, case, classes)}")
    for dep in spec["dependencies"]:
        if not _complete(index, plan, dep):
            reasons.append(f"dependency {dep} lacks complete evidence")
    return reasons


def _complete(index: dict, plan: dict, pid: str) -> bool:
    entry = index["packages"].get(pid)
    return bool(entry) and entry["claim"] == "complete" and not _missing(index, plan, pid)


def check(index: dict, plan: dict, root: Path | str) -> dict:
    findings = [
        {"package": pid, "reason": reason}
        for pid in plan["packages"]
        if index["packages"].get(pid, {}).get("claim") == "complete"
        for reason in _missing(index, plan, pid)
    ]
    missing = len({f["package"] for f in findings})
    return {
        "errors": validate(index, plan, root),
        "findings": findings,
        "measurements": {"packages_missing_evidence": missing},
    }


def digest(document: dict) -> str:
    return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()


def _write(path: Path | str, index: dict) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(index, indent=2) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _replayed(index: dict, operation: str, sha256: str) -> dict | None:
    done = next((o for o in index["operations"] if o["id"] == operation), None)
    if done and done["sha256"] != sha256:
        raise EvidenceError(f"operation {operation} was already recorded with different content")
    return done


def _commit(path: Path | str, index: dict, operation: str, sha256: str, result: dict) -> dict:
    index["operations"].append({"id": operation, "revision": index["revision"], "sha256": sha256, "result": result})
    _write(path, index)
    return result


def _raise_first(errors: list[str]) -> None:
    if errors:
        raise EvidenceError(errors[0])


def _recorded(index: dict) -> dict[str, dict]:
    return {e.get("id"): e for entry in index["packages"].values() for e in entry["evidence"]}


def record(path: Path | str, change: dict, plan: dict, root: Path | str) -> dict:
    index = load_index(path)
    operation, sha256 = change["operation"], digest(change)
    done = _replayed(index, operation, sha256)
    if done:
        return done["result"]
    if change["base_revision"] != index["revision"]:
        raise EvidenceError(
            f"change is based on revision {change['base_revision']}; the registry is at revision {index['revision']}"
        )
    pid = change["package"]
    recorded = _recorded(index)
    for item in change["evidence"]:
        if item.get("id") in recorded and recorded[item["id"]] != item:
            raise EvidenceError(f"evidence {item['id']} is already recorded with different content")
    entry = index["packages"].setdefault(pid, {"claim": "open", "evidence": []})
    added = [item for item in change["evidence"] if item.get("id") not in recorded]
    entry["evidence"] += added
    entry["claim"] = change.get("claim", entry["claim"])
    _raise_first(validate(index, plan, root))
    index["revision"] += 1
    result = {
        "operation": operation,
        "revision": index["revision"],
        "package": pid,
        "claim": entry["claim"],
        "added": [item["id"] for item in added],
    }
    return _commit(path, index, operation, sha256, result)


def _readable(path: Path | str) -> dict | None:
    try:
        return load_index(path)
    except (OSError, ValueError):
        return None


def restore(path: Path | str, backup: Path | str, operation: str, plan: dict, root: Path | str) -> dict:
    saved = load_index(backup)
    sha256 = digest({"restore": saved})
    current = _readable(path)
    done = current and _replayed(current, operation, sha256)
    if done:
        return done["result"]
    _raise_first(validate(saved, plan, root))
    if current and current["revision"] > saved["revision"]:
        raise EvidenceError(
            f"backup at revision {saved['revision']} is older than the registry at revision {current['revision']}"
        )
    if current and current["revision"] == saved["revision"] and digest(current) != digest(saved):
        raise EvidenceError(f"backup at revision {saved['revision']} differs from the registry at the same revision")
    index = copy.deepcopy(saved)
    index["revision"] += 1
    result = {
        "operation": operation,
        "revision": index["revision"],
        "restored_from": saved["revision"],
        "packages": len(index["packages"]),
        "evidence": len(_recorded(index)),
    }
    return _commit(path, index, operation, sha256, result)


def reopen(path: Path | str, package: str, operation: str) -> dict:
    index = load_index(path)
    sha256 = digest({"reopen": package})
    done = _replayed(index, operation, sha256)
    if done:
        return done["result"]
    entry = index["packages"].get(package, {})
    if entry.get("claim") != "complete":
        raise EvidenceError(f"{package} is not claimed complete")
    entry["claim"] = "open"
    index["revision"] += 1
    result = {"operation": operation, "revision": index["revision"], "package": package, "claim": "open"}
    return _commit(path, index, operation, sha256, result)


def _failed(index: dict) -> list[str]:
    return [
        f"- {pid}: " + ", ".join(p for p in (e["id"], e.get("case") and f"case {e['case']}", e["ref"]) if p)
        for pid, entry in index["packages"].items()
        for e in entry["evidence"]
        if e.get("outcome") == "failed"
    ]


def render(index: dict, plan: dict, result: dict) -> str:
    counts = {}
    for f in result["findings"]:
        counts[f["package"]] = counts.get(f["package"], 0) + 1
    lines = [
        "# Swarm v2 requirement-to-evidence registry",
        "",
        f"Package SV2-FND-04, registry revision {index['revision']}. Generated from `{INDEX}` by"
        " `python -m scripts.swarm_v2.validate_plan render`; edit the registry, never this file.",
        "",
        "## Gates",
        "",
        "| Gate | Packages |",
        "|---|---|",
        *[f"| {gate} | {', '.join(pids)} |" for gate, pids in gates(plan).items()],
        "",
        "## Invariants",
        "",
        "| Invariant | Class | Packages |",
        "|---|---|---|",
        *[
            f"| {r['id']} | {r['class'].replace('_', ' ')} | {', '.join(r['packages'])} |"
            for r in index["requirements"]
        ],
        "",
        "## Claimed packages",
        "",
        "| Package | Repository | Gate | Claim | Evidence | Findings |",
        "|---|---|---|---|---|---|",
    ]
    for pid, entry in index["packages"].items():
        spec = plan["packages"][pid]
        lines.append(
            f"| {pid} | {spec['repository']} | {spec['gate']} | {entry['claim']} | {len(entry['evidence'])} |"
            f" {counts.get(pid, 'none')} |"
        )
    findings = [f"- {f['package']}: {f['reason']}" for f in result["findings"]]
    lines += ["", "## Findings", "", *(findings or ["None."])]
    lines += ["", "## Failed experiments", "", *(_failed(index) or ["None."])]
    missing = result["measurements"]["packages_missing_evidence"]
    lines += ["", f"packages_missing_evidence: {missing}", ""]
    return "\n".join(lines)


def _render_to(args, plan: dict) -> None:
    index = load_index(args.index)
    Path(args.markdown).write_text(render(index, plan, check(index, plan, args.root)))


def _run(args) -> int:
    plan = load_plan(args.plan)
    if args.command == "check":
        result = check(load_index(args.index), plan, args.root)
        print(json.dumps(result, indent=2))
        return 1 if result["errors"] or result["findings"] else 0
    if args.command == "record":
        change = json.loads(Path(args.change).read_text())
        print(json.dumps(record(args.index, change, plan, args.root), indent=2))
    elif args.command == "restore":
        print(json.dumps(restore(args.index, args.backup, args.operation, plan, args.root), indent=2))
    elif args.command == "reopen":
        print(json.dumps(reopen(args.index, args.package, args.operation), indent=2))
    _render_to(args, plan)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.validate_plan")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "render", "record", "restore", "reopen"):
        command = commands.add_parser(name)
        command.add_argument("--index", default=INDEX)
        command.add_argument("--plan", default=PLAN)
        command.add_argument("--root", default=".")
        if name != "check":
            command.add_argument("--markdown", default=MARKDOWN)
        if name == "record":
            command.add_argument("--change", required=True)
        if name in ("restore", "reopen"):
            command.add_argument("--operation", required=True)
        if name == "restore":
            command.add_argument("--backup", required=True)
        if name == "reopen":
            command.add_argument("--package", required=True)
    args = parser.parse_args(argv)
    try:
        return _run(args)
    except EvidenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
