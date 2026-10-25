import json
import sys
import urllib.request

ACCEPT = ", ".join(
    f"application/vnd.{kind}+json"
    for kind in (
        "oci.image.index.v1",
        "oci.image.manifest.v1",
        "docker.distribution.manifest.list.v2",
        "docker.distribution.manifest.v2",
    )
)


def get(url: str, token: str | None = None) -> tuple[dict, str | None]:
    request = urllib.request.Request(url, headers={"Accept": ACCEPT})
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as reply:
        return json.load(reply), reply.headers.get("Docker-Content-Digest")


def pinned_revision(image: str) -> tuple[str, str]:
    name, _, tag = image.rpartition(":")
    registry, _, repository = name.partition("/")
    v2 = f"https://{registry}/v2/{repository}"
    token = get(f"https://{registry}/token?scope=repository:{repository}:pull")[0]["token"]
    manifest, digest = get(f"{v2}/manifests/{tag}", token)
    if "manifests" in manifest:
        digest = next(
            (m["digest"] for m in manifest["manifests"] if m.get("platform", {}).get("architecture") == "amd64"),
            None,
        )
        if not digest:
            sys.exit(f"{image} has no amd64 image")
        manifest = get(f"{v2}/manifests/{digest}", token)[0]
    config = get(f"{v2}/blobs/{manifest['config']['digest']}", token)[0]
    revision = (config["config"].get("Labels") or {}).get("org.opencontainers.image.revision")
    if not digest or not revision:
        sys.exit(f"{image} has no amd64 image with a revision label")
    return f"{name}@{digest}", revision


if __name__ == "__main__":
    print(*pinned_revision(sys.argv[1]))
