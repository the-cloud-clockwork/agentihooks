import argparse
import copy
import fcntl
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

SCHEMA = "swarm-v2-evidence-index/1"
PLAN = "Swarm-v2.md"
INDEX = "docs/swarm-v2/evidence-index.json"
MARKDOWN = "docs/swarm-v2/evidence-index.md"
CLASSES = ("transcript_durability", "scope_isolation", "execution_safety", "knowledge_integrity")
CLAIMS = ("open", "complete")
CHANGE_KEYS = ("operation", "base_revision", "package", "evidence")
INDEX_KEYS = ("revision", "requirements", "packages", "operations")
RESULT_SUFFIXES = (".json", ".log", ".txt", ".xml")
CASES = (("A", "positive case result"), ("B", "negative case result"), ("C", "recovery proof"))
PROVES = ("live_canary", "test")
NEEDS_COMMIT = frozenset({"test", "pull_request", "live_canary", "migration"})
NEEDS_CASE = frozenset({"test", "live_canary", "experiment"})
NEEDS_OUTCOME = frozenset({"test", "live_canary", "experiment", "migration"})
KINDS = tuple(sorted(NEEDS_COMMIT | NEEDS_CASE | {"screenshot"}))
CASE_IDS = tuple(case for case, _ in CASES)
REF = "ref must be an https link or a repository file with its sha256"
PACKAGE_RE = re.compile(r"^#### (SV2-[A-Z]+-\d\d): ", re.M)
PACKAGE_ID = re.compile(r"SV2-[A-Z]+-\d\d")
INVARIANT_RE = re.compile(r"^`(INV-[A-Z]\d\d)`:", re.M)
CASE_RE = re.compile(r"^- (?:Positive|Negative|Recovery) case: \[(T-SV2-[A-Z]+-\d\d-([ABC]))\]", re.M)
URL_RE = re.compile(r"https://\S+")
SHA_RE = re.compile(r"[0-9a-f]{40}")
RUN_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/actions/runs/\d+(?:/job/\d+)?")
RESULT = "a test or live canary result must be a GitHub Actions run or a text file in the repository"


class EvidenceError(ValueError):
    pass


def _field(section: str, name: str) -> str:
    match = re.search(rf"^- {name}: (.*)$", section, re.M)
    return match.group(1) if match else ""


def _package(pid: str, section: str) -> dict:
    gate = re.search(r"`(G\d+)`", _field(section, "Integration gate"))
    cases = {case: test for test, case in CASE_RE.findall(section)}
    if not gate or len(cases) != len(CASES):
        raise EvidenceError(f"{pid} in the plan lacks its integration gate or one of its three acceptance cases")
    return {
        "repository": _field(section, "Repository").strip("`."),
        "gate": gate.group(1),
        "dependencies": PACKAGE_ID.findall(_field(section, "Dependencies")),
        "cases": cases,
    }


def load_plan(path: Path | str) -> dict:
    text = Path(path).read_text()
    heads = list(PACKAGE_RE.finditer(text))
    ends = [h.start() for h in heads[1:]] + [len(text)]
    packages = {}
    for head, end in zip(heads, ends):
        section = text[head.end() : end].split("\n#### ", 1)[0]
        packages[head.group(1)] = _package(head.group(1), section)
    return {"packages": packages, "invariants": INVARIANT_RE.findall(text)}


def gates(plan: dict) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    for pid, spec in plan["packages"].items():
        grouped.setdefault(spec["gate"], []).append(pid)
    return {gate: grouped[gate] for gate in sorted(grouped, key=lambda g: int(g[1:]))}


def load_index(path: Path | str) -> dict:
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise EvidenceError(f"{path} is not a {SCHEMA} document")
    lacking = [key for key in INDEX_KEYS if key not in data]
    if lacking:
        raise EvidenceError(f"{path} lacks {', '.join(lacking)}")
    return data


def _ref_error(item: dict, root: Path) -> str:
    ref = str(item.get("ref", ""))
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


def _result_ref(ref: str, root: Path) -> bool:
    if RUN_RE.fullmatch(ref):
        return True
    if URL_RE.fullmatch(ref) or PurePosixPath(ref).suffix not in RESULT_SUFFIXES:
        return False
    data = (root / ref).read_bytes()
    try:
        data.decode()
    except UnicodeDecodeError:
        return False
    return b"\x00" not in data


def _item_errors(item: dict, root: Path) -> list[str]:
    kind = item.get("kind")
    if kind not in KINDS:
        return [f"unknown kind {kind!r}"]
    errors = [_ref_error(item, root)]
    if kind in PROVES and not errors[0] and not _result_ref(str(item["ref"]), root):
        errors.append(RESULT)
    if kind in NEEDS_COMMIT and not SHA_RE.fullmatch(str(item.get("commit", ""))):
        errors.append("commit must be a full 40 character sha")
    if (kind in NEEDS_CASE or "case" in item) and item.get("case") not in CASE_IDS:
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


def _identified(item: object) -> bool:
    return isinstance(item, dict) and isinstance(item.get("id"), str)


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
            if not _identified(item):
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
        re.fullmatch(rf"https://github\.com/[\w.-]+/{re.escape(repository)}/pull/\d+", str(item.get("ref", "")))
    )


def _screenshot_note(evidence: list[dict], case: str, classes: list[str]) -> str:
    if not any(e.get("kind") == "screenshot" and e.get("case", case) == case for e in evidence):
        return ""
    scope = f"{' and '.join(classes)} acceptance" if classes else "acceptance"
    return f"; a screenshot cannot satisfy {scope}"


def _missing(index: dict, plan: dict, pid: str, seen: tuple[str, ...] = ()) -> list[str]:
    spec = plan["packages"][pid]
    evidence = [e for e in index["packages"][pid]["evidence"] if isinstance(e, dict)]
    reasons = []
    tested = [e.get("commit") for e in evidence if _pull_request(e, spec["repository"])]
    if not tested:
        reasons.append("no pull request with a tested commit")
    classes = _classes(index, pid)
    for case, label in CASES:
        if not any(_proves(e, case) and (not tested or e.get("commit") in tested) for e in evidence):
            reasons.append(f"{spec['cases'][case]}: missing {label}{_screenshot_note(evidence, case, classes)}")
    for dep in spec["dependencies"]:
        if dep in seen or not _complete(index, plan, dep, (*seen, pid)):
            reasons.append(f"dependency {dep} lacks complete evidence")
    return reasons


def _complete(index: dict, plan: dict, pid: str, seen: tuple[str, ...]) -> bool:
    entry = index["packages"].get(pid)
    return bool(entry) and entry["claim"] == "complete" and not _missing(index, plan, pid, seen)


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


@contextmanager
def _locked(path: Path | str) -> Iterator[None]:
    path = Path(path)
    with path.with_name(f".{path.name}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


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
    return {e["id"]: e for entry in index["packages"].values() for e in entry["evidence"] if _identified(e)}


def _record(path: Path | str, change: dict, plan: dict, root: Path | str) -> dict:
    index = load_index(path)
    if not isinstance(change, dict):
        raise EvidenceError("change must be a JSON object")
    lacking = [key for key in CHANGE_KEYS if key not in change]
    if lacking:
        raise EvidenceError(f"change lacks {', '.join(lacking)}")
    operation, sha256 = change["operation"], digest(change)
    done = _replayed(index, operation, sha256)
    if done:
        return done["result"]
    if change["base_revision"] != index["revision"]:
        raise EvidenceError(
            f"change is based on revision {change['base_revision']}; the registry is at revision {index['revision']}"
        )
    pid = change["package"]
    if not all(_identified(item) for item in change["evidence"]):
        raise EvidenceError(f"{pid}: evidence needs an id")
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


def record(path: Path | str, change: dict, plan: dict, root: Path | str) -> dict:
    with _locked(path):
        return _record(path, change, plan, root)


def _readable(path: Path | str) -> dict | None:
    try:
        return load_index(path)
    except (OSError, ValueError):
        return None


def _replaced(path: Path | str, current: dict | None) -> str:
    if current:
        return f"revision {current['revision']}"
    return "unreadable" if Path(path).exists() else "missing"


def _restore(path: Path | str, backup: Path | str, operation: str, plan: dict, root: Path | str) -> dict:
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
    if current and saved["operations"][: len(current["operations"])] != current["operations"]:
        raise EvidenceError(f"backup at revision {saved['revision']} lacks operations the registry has recorded")
    if current and current["revision"] == saved["revision"] and digest(current) != digest(saved):
        raise EvidenceError(f"backup at revision {saved['revision']} differs from the registry at the same revision")
    index = copy.deepcopy(saved)
    index["revision"] += 1
    result = {
        "operation": operation,
        "revision": index["revision"],
        "restored_from": saved["revision"],
        "replaced": _replaced(path, current),
        "packages": len(index["packages"]),
        "evidence": len(_recorded(index)),
    }
    return _commit(path, index, operation, sha256, result)


def restore(path: Path | str, backup: Path | str, operation: str, plan: dict, root: Path | str) -> dict:
    with _locked(path):
        return _restore(path, backup, operation, plan, root)


def _reopen(path: Path | str, package: str, operation: str) -> dict:
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


def reopen(path: Path | str, package: str, operation: str) -> dict:
    with _locked(path):
        return _reopen(path, package, operation)


def _failed(index: dict) -> list[str]:
    return [
        f"- {pid}: " + ", ".join(p for p in (e["id"], e.get("case") and f"case {e['case']}", e["ref"]) if p)
        for pid, entry in index["packages"].items()
        for e in entry["evidence"]
        if _identified(e) and e.get("outcome") == "failed"
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
    except (EvidenceError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
