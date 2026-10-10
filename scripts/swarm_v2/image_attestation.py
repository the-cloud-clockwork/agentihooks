"""SV2-IMG-05: qualify a worker image's pinned targets and build the release attestation that alone promotes its digest."""

import argparse
import json
import os
import re
import sys
from pathlib import Path

PACKAGE = "SV2-IMG-05"
TARGETS = ("herdr", "claude", "codex")
HERDR_PROTOCOL = 22
HERDR_CAPABILITIES = ("detached_server_daemon", "health_check")
HERDR_METHODS = frozenset(
    {
        "agent.get",
        "agent.list",
        "agent.prompt",
        "agent.rename",
        "pane.close",
        "pane.list",
        "pane.process_info",
        "pane.read",
        "pane.send_keys",
        "pane.send_text",
        "pane.split",
        "pane.wait_for_output",
        "server.stop",
        "tab.close",
        "tab.create",
        "tab.list",
        "workspace.close",
        "workspace.create",
        "workspace.list",
    }
)
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
COMMIT = re.compile(r"[a-f0-9]{40}")
PROVENANCE = {
    "repository": "GITHUB_REPOSITORY",
    "workflow": "GITHUB_WORKFLOW_REF",
    "run_id": "GITHUB_RUN_ID",
    "run_attempt": "GITHUB_RUN_ATTEMPT",
    "ref": "GITHUB_REF",
    "runner": "RUNNER_ENVIRONMENT",
}


class Refused(ValueError):
    pass


def herdr_refusals(observed: dict) -> list[str]:
    status, schema = observed.get("status") or {}, observed.get("schema") or {}
    refusals = [] if status.get("running") is True else ["herdr headless server did not run"]
    if status.get("protocol") != HERDR_PROTOCOL or schema.get("protocol") != HERDR_PROTOCOL:
        refusals.append(f"herdr protocol is not {HERDR_PROTOCOL}")
    capabilities = status.get("capabilities") or {}
    refusals += [f"herdr server lacks {name}" for name in HERDR_CAPABILITIES if capabilities.get(name) is not True]
    missing = sorted(HERDR_METHODS - set(schema.get("methods") or ()))
    if missing:
        refusals.append("herdr socket API lacks " + ", ".join(missing))
    return refusals


def harness_refusals(name: str, observed: dict) -> list[str]:
    if observed.get("hook_registrations", 0) < 1:
        return [f"{name} headless launch delivered no SessionStart hook"]
    return []


def target_report(name: str, pinned: str, observed: dict) -> dict:
    found = observed.get("version")
    refusals = [] if found == pinned else [f"{name} version {found!r} is not pinned {pinned}"]
    refusals += herdr_refusals(observed) if name == "herdr" else harness_refusals(name, observed)
    return {"pinned": pinned, "observed": found, "qualified": not refusals, "refusals": refusals}


def qualify(manifest: dict, observed: dict) -> dict:
    pinned = manifest["observed"]["tools"]
    targets = {name: target_report(name, pinned.get(name, ""), observed.get(name) or {}) for name in TARGETS}
    qualified = sum(report["qualified"] for report in targets.values())
    return {
        "package": PACKAGE,
        "targets": targets,
        "worker_image_qualified_targets": qualified,
        "promotable": qualified == len(TARGETS),
    }


def compatibility(manifest: dict) -> dict:
    return {
        "herdr_protocol": HERDR_PROTOCOL,
        "herdr_server_capabilities": list(HERDR_CAPABILITIES),
        "herdr_methods": sorted(HERDR_METHODS),
        "targets": manifest["observed"]["tools"],
    }


def provenance(commit: str, environ: dict) -> dict:
    if not COMMIT.fullmatch(commit):
        raise Refused("provenance needs a full agentihooks commit")
    return {"commit": commit} | {key: environ.get(name, "") for key, name in PROVENANCE.items()}


def attest(probe: dict, image_id: str, origin: dict) -> dict:
    manifest = probe["manifest"]
    report = qualify(manifest, probe["observed"])
    refusals = [refusal for target in report["targets"].values() for refusal in target["refusals"]]
    if manifest.get("source_revision") != origin["commit"]:
        refusals.append("image manifest names another commit")
    if not DIGEST.fullmatch(image_id):
        refusals.append("tested image id is not a sha256 digest")
    return {
        "schema_version": 1,
        "package": PACKAGE,
        "image_id": image_id,
        "digest": None,
        "tags": [],
        "promotable": not refusals,
        "promoted": False,
        "refusals": refusals,
        "manifest": manifest,
        "report": report,
        "compatibility": compatibility(manifest),
        "provenance": origin,
    }


def _accepted(attestation: dict, digest: str) -> None:
    if not attestation["promotable"]:
        raise Refused("unqualified image: " + "; ".join(attestation["refusals"]))
    if not DIGEST.fullmatch(digest):
        raise Refused("registry digest is not a sha256 digest")


def promote(attestation: dict, digest: str, config: str, tags: list[str]) -> dict:
    _accepted(attestation, digest)
    if config != attestation["image_id"]:
        raise Refused("pushed image is not the tested image")
    return attestation | {"digest": digest, "tags": list(tags), "promoted": True, "replayed": False}


def replay(attestation: dict, digest: str, tags: list[str]) -> dict:
    _accepted(attestation, digest)
    return attestation | {"digest": digest, "tags": list(tags), "promoted": False, "replayed": True}


def _write(path: Path, body: dict) -> None:
    path.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _attest(args: argparse.Namespace) -> int:
    probe = json.loads(args.probe.read_text(encoding="utf-8"))
    made = attest(probe, args.image_id, provenance(args.commit, os.environ))
    _write(args.output, made)
    verdict = "promotable" if made["promotable"] else "refused: " + "; ".join(made["refusals"])
    print(f"qualified {made['report']['worker_image_qualified_targets']} of {len(TARGETS)} targets; {verdict}")
    return 0 if made["promotable"] else 1


def _promote(args: argparse.Namespace) -> int:
    made = json.loads(args.attestation.read_text(encoding="utf-8"))
    try:
        if args.existing:
            done, verb = replay(made, args.digest, args.tag), "kept accepted"
        else:
            done, verb = promote(made, args.digest, args.config, args.tag), "promoted"
    except Refused as refused:
        print(refused, file=sys.stderr)
        return 1
    _write(args.output, done)
    print(f"{PACKAGE} {verb} {args.digest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm_v2.image_attestation")
    commands = parser.add_subparsers(dest="command", required=True)
    made = commands.add_parser("attest")
    made.add_argument("--probe", type=Path, required=True)
    made.add_argument("--image-id", required=True)
    made.add_argument("--commit", required=True)
    made.add_argument("--output", type=Path, required=True)
    pushed = commands.add_parser("promote")
    pushed.add_argument("--attestation", type=Path, required=True)
    pushed.add_argument("--digest", required=True)
    pushed.add_argument("--config", required=True)
    pushed.add_argument("--tag", action="append", default=[])
    pushed.add_argument("--existing", action="store_true")
    pushed.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _attest(args) if args.command == "attest" else _promote(args)


if __name__ == "__main__":
    sys.exit(main())
