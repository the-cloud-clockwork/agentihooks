import json
import sys
import urllib.request

name, _, tag = sys.argv[1].rpartition(":")
registry, _, repository = name.partition("/")
accept = ", ".join(
    f"application/vnd.{kind}+json"
    for kind in (
        "oci.image.index.v1",
        "oci.image.manifest.v1",
        "docker.distribution.manifest.list.v2",
        "docker.distribution.manifest.v2",
    )
)


def get(path, token=None):
    request = urllib.request.Request(f"https://{registry}{path}", headers={"Accept": accept})
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=30) as reply:
        return json.load(reply), reply.headers.get("Docker-Content-Digest")


token = get(f"/token?scope=repository:{repository}:pull")[0]["token"]
manifest, digest = get(f"/v2/{repository}/manifests/{tag}", token)
if "manifests" in manifest:
    digest = next(
        (m["digest"] for m in manifest["manifests"] if m.get("platform", {}).get("architecture") == "amd64"),
        None,
    )
    if not digest:
        sys.exit(f"{sys.argv[1]} has no amd64 image")
    manifest = get(f"/v2/{repository}/manifests/{digest}", token)[0]
config = get(f"/v2/{repository}/blobs/{manifest['config']['digest']}", token)[0]
revision = (config["config"].get("Labels") or {}).get("org.opencontainers.image.revision")
if not digest or not revision:
    sys.exit(f"{sys.argv[1]} has no amd64 image with a revision label")
print(f"{name}@{digest}", revision)
