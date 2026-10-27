"""Place the swarm brain binding and its two client credentials in the agentihooks home, printing no value.

The binding comes from the swarm-brain-binding ConfigMap or a file; the credentials from the OpenBao record it names.
"""

import argparse
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TextIO

from scripts import openbao_kv, operator_env
from scripts.swarm import brain_binding, stack_env

ENV_FILE = "swarm-brain.env"
CONFIGMAP = [
    "kubectl",
    "get",
    "configmap",
    "swarm-brain-binding",
    "-n",
    "swarm-brain",
    "-o",
    "jsonpath={.data.binding\\.json}",
]


def from_cluster() -> str:
    try:
        done = subprocess.run(CONFIGMAP, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        done = None
    if done is None or done.returncode or not done.stdout.strip():
        raise brain_binding.Refused("the swarm-brain-binding ConfigMap could not be read")
    return done.stdout


def _references(doc: dict) -> tuple[str, dict[str, str]]:
    credentials = doc.get("credentials")
    record = credentials.get("record") if isinstance(credentials, dict) else None
    if not isinstance(record, str) or not record:
        raise brain_binding.Refused("the binding names no OpenBao record")
    wanted = {}
    for kind in brain_binding.KINDS:
        spec = credentials.get(kind)
        prop = spec.get("property") if isinstance(spec, dict) else None
        if not isinstance(prop, str) or not prop:
            raise brain_binding.Refused(f"the binding names no {kind} property")
        wanted[brain_binding.credential(doc, kind)[f"{kind}_env"]] = prop
    return record, wanted


def _write(target: Path, text: str) -> None:
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    staged = target.with_name(f".{target.name}.next")
    stack_env.write_private(staged, text)
    os.replace(staged, target)


def _home(environ: Mapping[str, str]) -> Path:
    return Path(environ.get("AGENTIHOOKS_HOME") or Path(environ.get("HOME") or Path.home()) / ".agentihooks")


def _staged(binding_text: str, bao: openbao_kv.OpenBao) -> tuple[dict, dict[str, str]]:
    try:
        doc = json.loads(binding_text)
    except ValueError:
        doc = None
    if not isinstance(doc, dict):
        raise brain_binding.Refused("the binding is not a JSON object")
    record, wanted = _references(doc)
    found, _ = openbao_kv.read(bao, record)
    missing = [prop for prop in wanted.values() if not isinstance(found.get(prop), str) or not found[prop]]
    if missing:
        raise brain_binding.Refused(f"OpenBao {record} lacks {', '.join(missing)}")
    return doc, {name: found[prop] for name, prop in wanted.items()}


def place(environ: Mapping[str, str], bao: openbao_kv.OpenBao, binding_text: str, out: TextIO) -> int:
    try:
        doc, values = _staged(binding_text, bao)
    except (ValueError, openbao_kv.OpenBaoError) as exc:
        print(f"brain-place: {exc}; nothing was written", file=out)
        return 1
    binding_file, env_file = brain_binding.path(environ), _home(environ) / ENV_FILE
    _write(binding_file, json.dumps(doc, indent=2, sort_keys=True) + "\n")
    _write(env_file, "".join(f"{name}={shlex.quote(value)}\n" for name, value in values.items()))
    for written in (binding_file, env_file):
        print(f"{written.name} {written.stat().st_mode & 0o777:04o}", file=out)
    decided = brain_binding.decide(environ, operator_env.values(environ))
    reason = f": {decided['reason']}" if decided["status"] != brain_binding.BOUND else ""
    print(f"brain binding {decided['status']}{reason}", file=out)
    return 0 if decided["status"] == brain_binding.BOUND else 1


def main(
    argv: list[str],
    environ: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    opener: Callable | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="agentihooks swarm brain-place", description=__doc__)
    parser.add_argument("--binding", type=Path, help="binding JSON file instead of the swarm-brain-binding ConfigMap")
    args = parser.parse_args(argv)
    environ = os.environ if environ is None else environ
    out = sys.stdout if out is None else out
    bao = openbao_kv.from_env(environ, opener)
    try:
        if bao is None:
            raise brain_binding.Refused("OPENBAO_URL and OPENBAO_TOKEN must be set")
        if args.binding is None:
            text = from_cluster()
        else:
            try:
                text = args.binding.read_text()
            except (OSError, ValueError):
                raise brain_binding.Refused("the binding file could not be read") from None
    except brain_binding.Refused as exc:
        print(f"brain-place: {exc}; nothing was written", file=out)
        return 1
    return place(environ, bao, text, out)
