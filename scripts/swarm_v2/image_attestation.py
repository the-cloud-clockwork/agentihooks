import argparse
import json
import os
import re
import sys
from pathlib import Path

from scripts.swarm_v2.herdr import capabilities

PACKAGE = "SV2-IMG-05"
SCHEMA_VERSION = 1
TARGETS = ("herdr", "claude", "codex")
HERDR_PROTOCOL = 22
HERDR_CAPABILITIES = capabilities.SERVER_CAPABILITIES
HERDR_BASELINE = "local herdr 0.9.1 runtime path"
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
        "pane.send_input",
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


def capability_refusals(capabilities: dict) -> list[str]:
    return [
        f"herdr server lacks {name}={value}"
        for name, value in HERDR_CAPABILITIES.items()
        if type(capabilities.get(name)) is not type(value) or capabilities.get(name) != value
    ]


def herdr_refusals(observed: dict) -> list[str]:
    status, schema = observed.get("status") or {}, observed.get("schema") or {}
    refusals = [] if status.get("running") is True else ["herdr headless server did not run"]
    if status.get("protocol") != HERDR_PROTOCOL or schema.get("protocol") != HERDR_PROTOCOL:
        refusals.append(f"herdr protocol is not {HERDR_PROTOCOL}")
    refusals += capability_refusals(status.get("capabilities") or {})
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
    refusals = [] if pinned and found == pinned else [f"{name} version {found!r} is not pinned {pinned!r}"]
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
        "baseline": HERDR_BASELINE,
        "herdr_protocol": HERDR_PROTOCOL,
        "herdr_server_capabilities": dict(HERDR_CAPABILITIES),
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
        "schema_version": SCHEMA_VERSION,
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


def promote(attestation: dict, digest: str, config: str, tags: list[str], replayed: bool = False) -> dict:
    if not attestation["promotable"]:
        raise Refused("unqualified image: " + "; ".join(attestation["refusals"]))
    if not DIGEST.fullmatch(digest):
        raise Refused("registry digest is not a sha256 digest")
    if config != attestation["image_id"]:
        raise Refused("registry image is not the tested image")
    return attestation | {
        "digest": digest,
        "config": config,
        "tags": list(tags),
        "promoted": not replayed,
        "replayed": replayed,
    }


def _write(path: str, body: dict) -> None:
    Path(path).write_text(json.dumps(body, indent=2, sort_keys=True) + "\n")


def _attest(args: argparse.Namespace) -> int:
    probe = json.loads(Path(args.probe).read_bytes())
    made = attest(probe, args.image_id, provenance(args.commit, os.environ))
    _write(args.output, made)
    verdict = "promotable" if made["promotable"] else "refused: " + "; ".join(made["refusals"])
    print(f"qualified {made['report']['worker_image_qualified_targets']} of {len(TARGETS)} targets; {verdict}")
    return 0 if made["promotable"] else 1


def _promote(args: argparse.Namespace) -> int:
    made = json.loads(Path(args.attestation).read_bytes())
    try:
        done = promote(made, args.digest, args.config, args.tag, args.existing)
    except Refused as refused:
        print(refused, file=sys.stderr)
        return 1
    _write(args.output, done)
    print(f"{PACKAGE} {'kept accepted' if args.existing else 'promoted'} {args.digest}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    made = commands.add_parser("attest")
    for name in ("probe", "image_id", "commit", "output"):
        made.add_argument(name)
    pushed = commands.add_parser("promote")
    for name in ("attestation", "digest", "config", "output"):
        pushed.add_argument(name)
    pushed.add_argument("--tag", action="append", default=[])
    pushed.add_argument("--existing", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _attest(args) if args.command == "attest" else _promote(args)


if __name__ == "__main__":
    sys.exit(main())
