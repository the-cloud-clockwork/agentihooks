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
    headers = {"Accept": accept, **({"Authorization": f"Bearer {token}"} if token else {})}
    with urllib.request.urlopen(
        urllib.request.Request(f"https://{registry}{path}", headers=headers), timeout=30
    ) as reply:
        return json.load(reply)


token = get(f"/token?scope=repository:{repository}:pull")["token"]
manifest = get(f"/v2/{repository}/manifests/{tag}", token)
digest = next(
    (m["digest"] for m in manifest.get("manifests", []) if m.get("platform", {}).get("architecture") == "amd64"),
    None,
)
if digest:
    manifest = get(f"/v2/{repository}/manifests/{digest}", token)
config = get(f"/v2/{repository}/blobs/{manifest['config']['digest']}", token)
revision = (config["config"].get("Labels") or {}).get("org.opencontainers.image.revision")
if not digest or not revision:
    sys.exit(f"{sys.argv[1]} has no amd64 image with a revision label")
print(f"{name}@{digest}", revision)
