import argparse
import json
import subprocess
import tempfile
from pathlib import Path

PIP_AUDIT = "pip-audit==2.10.1"
PYTHON = "3.12"
EXCLUDES = ".github/test-excludes.txt"


class AuditError(RuntimeError):
    pass


def resolve(root: Path, out: Path) -> Path:
    cmd = ["uv", "pip", "compile", str(root / "pyproject.toml"), "--all-extras", "--python-version", PYTHON]
    cmd += ["--no-header", "--no-annotate", "--quiet", "-o", str(out)]
    if (root / EXCLUDES).is_file():
        cmd += ["--excludes", str(root / EXCLUDES)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AuditError(f"resolving {root} failed: {result.stderr.strip()}")
    return out


def audit(requirements: Path) -> str:
    cmd = ["uvx", PIP_AUDIT, "-r", str(requirements), "--no-deps", "--disable-pip", "--format", "json"]
    return subprocess.run(cmd, capture_output=True, text=True).stdout


def findings(report: str) -> set[tuple[str, str, str]]:
    try:
        dependencies = json.loads(report)["dependencies"]
        return {(dep["name"], dep["version"], vuln["id"]) for dep in dependencies for vuln in dep.get("vulns", [])}
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as error:
        raise AuditError(f"pip-audit produced no report: {error!r}") from error


def new_findings(head: set, base: set) -> list[tuple[str, str, str]]:
    known = {(name, vuln) for name, _, vuln in base}
    return sorted(f for f in head if (f[0], f[2]) not in known)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on known vulnerabilities new against the base revision.")
    parser.add_argument("--base", type=Path, required=True)
    parser.add_argument("--head", type=Path, required=True)
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory() as folder:
        head = findings(audit(resolve(args.head, Path(folder, "head"))))
        base = findings(audit(resolve(args.base, Path(folder, "base"))))
    new = new_findings(head, base)
    print(f"{len(head)} known vulnerabilities on head, {len(base)} on base, {len(new)} new.")
    for name, version, vuln in new:
        print(f"::error::{name} {version} carries {vuln}")
    return 1 if new else 0


if __name__ == "__main__":
    raise SystemExit(main())
