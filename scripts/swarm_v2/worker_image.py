import argparse
import hashlib
import importlib.metadata
import io
import json
import platform
import re
import subprocess
import tarfile
import urllib.request
from pathlib import Path


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_artifact(name: str, artifact: dict) -> None:
    version = artifact.get("version", "")
    if (
        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
        or not re.fullmatch(r"[a-f0-9]{64}", artifact.get("sha256") or "")
        or not artifact.get("url", "").startswith("https://")
        or version not in re.findall(r"[0-9]+\.[0-9]+\.[0-9]+", artifact["url"])
        or re.findall(r"[0-9]+\.[0-9]+\.[0-9]+", artifact.get("version_output", "")) != [version]
    ):
        raise ValueError(f"invalid artifact lock: {name}")


def load_lock(path: Path, architecture: str) -> dict:
    lock = json.loads(path.read_text(encoding="utf-8"))
    if lock["schema_version"] != 1:
        raise ValueError("unsupported lock schema")
    if architecture not in lock["architectures"]:
        raise ValueError("unsupported architecture")
    if not re.fullmatch(r"python:[^@]+@sha256:[a-f0-9]{64}", lock["base_image"]):
        raise ValueError("base image requires a sha256 digest")
    if checksum(path.with_name("requirements.lock")) != lock["requirements_sha256"]:
        raise ValueError("Python requirements checksum mismatch")
    for name in ("herdr", "claude", "codex"):
        validate_artifact(name, lock["tools"].get(name, {}))
    return lock


def install_tools(lock: dict, destination: Path) -> None:
    binaries = {}
    for name, artifact in lock["tools"].items():
        with urllib.request.urlopen(artifact["url"], timeout=120) as response:
            payload = response.read()
        if hashlib.sha256(payload).hexdigest() != artifact["sha256"]:
            raise ValueError(f"artifact checksum mismatch: {name}")
        if "member" in artifact:
            with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
                payload = archive.extractfile(artifact["member"]).read()
        binaries[name] = payload
    for name, payload in binaries.items():
        target = destination / name
        target.write_bytes(payload)
        target.chmod(0o755)
    tool_versions(lock, destination)


def tool_versions(lock: dict, destination: Path) -> dict:
    versions = {}
    for name, artifact in lock["tools"].items():
        result = subprocess.run([str(destination / name), "--version"], check=True, capture_output=True, text=True)
        if result.stdout.strip() != artifact["version_output"]:
            raise ValueError(f"binary version mismatch: {name}")
        versions[name] = result.stdout.strip()
    return versions


def declared_inventory(path: Path, architecture: str) -> dict:
    lock = load_lock(path, architecture)
    return {
        "architecture": architecture,
        "base_image": lock["base_image"],
        "python": lock["python"],
        "debian_snapshot": lock["debian_snapshot"],
        "shell_packages": lock["shell_packages"],
        "tools": {name: artifact["version"] for name, artifact in lock["tools"].items()},
        "requirements_sha256": lock["requirements_sha256"],
        "lock_sha256": checksum(path),
    }


def observed_inventory(lock: dict, binaries: Path) -> dict:
    if platform.python_version() != lock["python"]:
        raise ValueError("Python version mismatch")
    packages = subprocess.run(
        ["dpkg-query", "-W", "-f=${Package}=${Version}\n"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    installed = dict(line.split("=", 1) for line in packages)
    if any(installed.get(name) != version for name, version in lock["shell_packages"].items()):
        raise ValueError("shell package version mismatch")
    return {
        "python": platform.python_version(),
        "tools": tool_versions(lock, binaries),
        "debian_packages": sorted(packages),
        "python_packages": sorted(f"{p.metadata['Name']}=={p.version}" for p in importlib.metadata.distributions()),
    }


def write_manifest(path: Path, architecture: str, source_revision: str, templates: Path) -> None:
    manifest = {
        "schema_version": 1,
        "package": "SV2-IMG-01",
        "source_revision": source_revision,
        "declared": declared_inventory(path, architecture),
        "observed": observed_inventory(load_lock(path, architecture), Path("/usr/local/bin")),
        "profile_templates": {
            str(p.relative_to(templates)): checksum(p) for p in sorted(templates.rglob("*")) if p.is_file()
        },
    }
    path.with_name("manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def report(path: Path) -> dict:
    manifest = json.loads(path.with_name("manifest.json").read_text(encoding="utf-8"))
    declared = declared_inventory(path, manifest["declared"]["architecture"])
    observed = observed_inventory(load_lock(path, declared["architecture"]), Path("/usr/local/bin"))
    if declared != manifest["declared"] or observed != manifest["observed"]:
        raise ValueError("installed inventory differs from build manifest")
    return {"package": "SV2-IMG-01", "worker_image_build_validation_failures": 0, "manifest": manifest}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("validate", "install", "manifest", "report", "shell-packages"))
    parser.add_argument("--lock", type=Path, default=Path("/opt/swarm-node/versions.lock"))
    parser.add_argument("--architecture", default="amd64")
    parser.add_argument("--base-image")
    parser.add_argument("--source-revision", default="unknown")
    args = parser.parse_args()
    lock = load_lock(args.lock, args.architecture)
    if args.base_image is not None and args.base_image != lock["base_image"]:
        raise ValueError("base image differs from lock")
    if args.action == "install":
        install_tools(lock, Path("/usr/local/bin"))
    elif args.action == "manifest":
        write_manifest(args.lock, args.architecture, args.source_revision, Path("/opt/agentihooks/templates"))
    elif args.action == "report":
        print(json.dumps(report(args.lock), sort_keys=True))
    elif args.action == "shell-packages":
        print(" ".join(f"{name}={version}" for name, version in lock["shell_packages"].items()))


if __name__ == "__main__":
    main()
