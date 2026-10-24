#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/../.."
proof_dir="$(mktemp -d "$PWD/.hive-attestation.XXXXXX")"
label="hive-attestation-proof=${proof_dir##*.}"
registry="hive-attestation-registry-${proof_dir##*.}"
daemon="hive-attestation-daemon-${proof_dir##*.}"
original_docker_host="${DOCKER_HOST:-unix:///var/run/docker.sock}"
cleanup() {
  docker --host "$original_docker_host" ps -aq --filter "label=$label" | xargs -r docker --host "$original_docker_host" rm -f
  rm -f "$proof_dir/override.yaml" "$proof_dir/pull.log"
  rmdir "$proof_dir"
}
trap cleanup EXIT
docker run -d --name "$registry" --label "$label" --platform linux/amd64 -p 5000:5000 mirror.gcr.io/library/python:3.12-slim-bookworm python -u -c "$(cat <<'PY'
import hashlib
import json
from http.server import BaseHTTPRequestHandler, HTTPServer

objects = {}

def store(value, media_type):
    body = json.dumps(value, separators=(",", ":")).encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    objects[digest] = (body, media_type)
    return {"mediaType": media_type, "digest": digest, "size": len(body)}

config = store({"architecture": "amd64", "os": "linux", "config": {},
                "rootfs": {"type": "layers", "diff_ids": []}, "history": []},
               "application/vnd.oci.image.config.v1+json")
image = store({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
               "config": config, "layers": []}, "application/vnd.oci.image.manifest.v1+json")
image["platform"] = {"architecture": "amd64", "os": "linux"}
absent = "sha256:" + "1" * 64
attestation = store({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                     "config": config, "layers": [{"mediaType": "application/vnd.in-toto+json",
                     "digest": absent, "size": 100}]}, "application/vnd.oci.image.manifest.v1+json")
attestation["platform"] = {"architecture": "unknown", "os": "unknown"}
attestation["annotations"] = {"vnd.docker.reference.type": "attestation-manifest",
                              "vnd.docker.reference.digest": image["digest"]}
for tag, child in (("present", image), ("missing-image", {**image, "digest": absent})):
    index = store({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                   "manifests": [child, attestation]}, "application/vnd.oci.image.index.v1+json")
    objects[tag] = objects[index["digest"]]

class Registry(BaseHTTPRequestHandler):
    default_request_version = "HTTP/1.1"

    def do_HEAD(self):
        self.respond(False)

    def do_GET(self):
        self.respond(True)

    def respond(self, include_body):
        if self.path == "/v2/":
            body, media_type = b"{}", "application/json"
        else:
            entry = objects.get(self.path.rsplit("/", 1)[-1])
            if entry is None:
                self.send_error(404, "planted content absent")
                return
            body, media_type = entry
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Docker-Content-Digest", "sha256:" + hashlib.sha256(body).hexdigest())
        self.end_headers()
        if include_body:
            self.wfile.write(body)

HTTPServer(("0.0.0.0", 5000), Registry).serve_forever()
PY
)" > /dev/null &
registry_start=$!
docker run -d --name "$daemon" --label "$label" --privileged --network host --entrypoint dockerd mirror.gcr.io/library/docker:29-dind \
  --host=tcp://127.0.0.1:2377 --tls=false --insecure-registry=127.0.0.1:5000 > /dev/null &
daemon_start=$!
wait "$registry_start"
wait "$daemon_start"
for attempt in {1..30}; do
  if docker exec "$registry" python -c 'import urllib.request; urllib.request.urlopen("http://localhost:5000/v2/")' 2>/dev/null; then break; fi
  sleep 1
done
docker exec "$registry" python -c 'import urllib.request; urllib.request.urlopen("http://localhost:5000/v2/")'
proof_docker_host=tcp://127.0.0.1:2377
for attempt in {1..30}; do
  if docker --host "$proof_docker_host" info > /dev/null 2>&1; then break; fi
  sleep 1
done
docker --host "$proof_docker_host" info --format '{{json .DriverStatus}} {{json .RegistryConfig.IndexConfigs}}'
fixture=tests/integration/hive_launch/compose.yaml
DOCKER_HOST="$proof_docker_host" PULL_RETRY_SECONDS=1 bash scripts/packaging/pull-runtime-image.sh \
  127.0.0.1:5000/proof:missing-image --insecure > "$proof_dir/pull.log" 2>&1 &
negative=$!
printf 'services:\n  redis:\n    image: 127.0.0.1:5000/proof:present\n' > "$proof_dir/override.yaml"
DOCKER_HOST="$proof_docker_host" bash scripts/packaging/pull-runtime-image.sh 127.0.0.1:5000/proof:present --insecure
docker --host "$proof_docker_host" compose -f "$fixture" -f "$proof_dir/override.yaml" pull --policy missing redis
docker --host "$proof_docker_host" image inspect 127.0.0.1:5000/proof:present --format '{{.Architecture}}' | grep -x amd64
printf 'Absent attestation: hive launch image pull passed\n'
if wait "$negative"; then
  cat "$proof_dir/pull.log"
  printf 'Missing runnable image unexpectedly passed\n' >&2
  exit 1
fi
cat "$proof_dir/pull.log"
grep -E 'not found|404|absent' "$proof_dir/pull.log"
grep -c 'failed on attempt' "$proof_dir/pull.log" | grep -x 2
printf 'Missing runnable image: hive launch image pull failed as required\n'
