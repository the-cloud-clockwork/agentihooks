import argparse
import json
import subprocess
import tempfile
from pathlib import Path

PIP_AUDIT = "pip-audit==2.10.1"
PYTHON = "3.12"

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


def audit(requirements: Path) -> str:
    cmd = ["uvx", PIP_AUDIT, "-r", str(requirements), "--no-deps", "--disable-pip", "--format", "json"]
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def parse(report: str) -> tuple[set[Finding], set[str]]:
    try:
        dependencies = json.loads(report)["dependencies"]
        found = {(dep["name"], dep["version"], vuln["id"]) for dep in dependencies for vuln in dep.get("vulns", [])}
        skipped = {dep["name"] for dep in dependencies if "skip_reason" in dep}
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
        raise AuditError(f"pip-audit produced no report: {error!r}") from error
    return found, skipped


def new_findings(head: set[Finding], base: set[Finding]) -> list[Finding]:
    known = {(name, vuln) for name, _, vuln in base}
    return sorted(f for f in head if (f[0], f[2]) not in known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on known vulnerabilities new against the base revision.")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as folder:
        head, head_skipped = parse(audit(resolve(args.head, Path(folder, "head"))))
        base, base_skipped = parse(audit(resolve(args.base, Path(folder, "base"))))
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
