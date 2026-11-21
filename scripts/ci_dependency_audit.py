import argparse
import json
import subprocess
import tempfile
from pathlib import Path

PIP_AUDIT = "pip-audit==2.10.1"
PYTHON = "3.12"
STDERR_TAIL_LINES = 40

Finding = tuple[str, str, str]


class AuditError(RuntimeError):
    pass


def resolve(root: Path, out: Path) -> Path:
    cmd = ["uv", "pip", "compile", str(root / "pyproject.toml"), "--all-extras", "--python-version", PYTHON]
    cmd += ["--no-header", "--no-annotate", "--quiet", "-o", str(out)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AuditError(f"resolving {root} failed: {result.stderr.strip()}")
    return out


def _stderr_tail(result: subprocess.CompletedProcess[str]) -> str:
    return "\n".join(result.stderr.splitlines()[-STDERR_TAIL_LINES:])


def _empty_report_diagnosis(result: subprocess.CompletedProcess[str]) -> str:
    tail = _stderr_tail(result)
    return f"exit {result.returncode}" + (f", stderr: {tail}" if tail else "")


def _print_empty_report_diagnosis(result: subprocess.CompletedProcess[str]) -> None:
    print(f"::warning::pip-audit exited {result.returncode}")
    tail = _stderr_tail(result)
    if tail:
        print(tail)


def audit(requirements: Path) -> subprocess.CompletedProcess[str]:
    cmd = ["uvx", PIP_AUDIT, "-r", str(requirements), "--no-deps", "--disable-pip", "--format", "json"]
    return subprocess.run(cmd, capture_output=True, text=True)


def parse(report: str) -> tuple[set[Finding], set[str]]:
    try:
        dependencies = json.loads(report)["dependencies"]
        found = {(dep["name"], dep["version"], vuln["id"]) for dep in dependencies for vuln in dep.get("vulns", [])}
        skipped = {dep["name"] for dep in dependencies if "skip_reason" in dep}
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
        raise AuditError(f"pip-audit produced no report: {error!r}") from error
    return found, skipped


def audited(requirements: Path) -> tuple[set[Finding], set[str]]:
    result = audit(requirements)
    if result.stdout.strip():
        return parse(result.stdout)
    print(f"::warning::pip-audit produced an empty report; auditing {requirements.name} once more")
    _print_empty_report_diagnosis(result)
    result = audit(requirements)
    if not result.stdout.strip():
        _print_empty_report_diagnosis(result)
        raise AuditError(f"pip-audit produced no report: {_empty_report_diagnosis(result)}")
    return parse(result.stdout)


def new_findings(head: set[Finding], base: set[Finding]) -> list[Finding]:
    known = {(name, vuln) for name, _, vuln in base}
    return sorted(f for f in head if (f[0], f[2]) not in known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on known vulnerabilities new against the base revision.")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as folder:
        head, head_skipped = audited(resolve(args.head, Path(folder, "head")))
        base, base_skipped = audited(resolve(args.base, Path(folder, "base")))
    new = new_findings(head, base)
    unaudited = sorted(head_skipped - base_skipped)
    print(f"{len(head)} known vulnerabilities on head, {len(base)} on base, {len(new)} new.")
    print(f"{len(head_skipped)} dependencies not audited on head, {len(base_skipped)} on base, {len(unaudited)} new.")
    for name, version, vuln in new:
        print(f"::error::{name} {version} carries {vuln}")
    for name in unaudited:
        print(f"::error::{name} could not be audited")
    return 1 if new or unaudited else 0


if __name__ == "__main__":
    raise SystemExit(main())
