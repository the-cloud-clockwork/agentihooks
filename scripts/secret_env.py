import base64
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


def _target(raw: str) -> Path:
    home = Path(os.environ.get("AGENTIHOOKS_HOME", str(Path.home() / ".agentihooks"))).expanduser().resolve()
    target = Path(raw).expanduser()
    if not target.is_absolute():
        target = home / target
    resolved = target.resolve()
    if resolved == home or not resolved.is_relative_to(home):
        raise ValueError
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise ValueError
    if target.name != ".env" and target.suffix != ".env":
        raise ValueError
    if target.exists() and not target.is_file():
        raise ValueError
    if not target.parent.is_dir():
        raise ValueError
    return target


def _read_value(namespace: str, secret: str, key: str) -> str:
    result = subprocess.run(
        [
            "kubectl",
            "get",
            "secret",
            secret,
            "--namespace",
            namespace,
            "-o",
            f'go-template={{{{index .data "{key}"}}}}',
        ],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode:
        raise ValueError
    value = base64.b64decode(result.stdout, validate=True).decode("utf-8")
    if any(character in value for character in "\r\n\x00"):
        raise ValueError
    return value


def _write_variable(target: Path, variable: str, value: str) -> None:
    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    pattern = re.compile(rf"^\s*(?:export\s+)?{variable}\s*=")
    matches = [index for index, line in enumerate(lines) if pattern.match(line)]
    position = matches[0] if matches else len(lines)
    kept = [line for index, line in enumerate(lines) if index not in matches]
    kept.insert(position, f"{variable}={shlex.quote(value)}")
    descriptor, name = tempfile.mkstemp(dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(kept) + "\n")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        print("Usage: agentihooks secret-env NAMESPACE SECRET KEY VARIABLE FILE", file=sys.stderr)
        return 2
    namespace, secret, key, variable, raw = argv
    try:
        if not all(re.fullmatch(r"[a-z0-9](?:[-a-z0-9.]*[a-z0-9])?", name) for name in (namespace, secret)):
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9._-]+", key) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
            raise ValueError
        target = _target(raw)
        value = _read_value(namespace, secret, key)
        _write_variable(target, variable, value)
    except (OSError, ValueError, subprocess.SubprocessError):
        print("Secret transfer failed.", file=sys.stderr)
        return 1
    return 0
