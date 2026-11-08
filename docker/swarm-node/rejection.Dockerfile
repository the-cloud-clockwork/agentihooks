ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
USER root
COPY docker/swarm-node/versions.lock /opt/swarm-node/versions.lock
RUN --network=none python -c 'import sys; from pathlib import Path; sys.path.insert(0, "/opt/swarm-node"); from worker_image import load_lock, fetch; lock = load_lock(Path("/opt/swarm-node/versions.lock"), "amd64"); fetch("herdr", {**lock["tools"]["herdr"], "url": Path("/usr/local/bin/herdr").as_uri()}, None)'
