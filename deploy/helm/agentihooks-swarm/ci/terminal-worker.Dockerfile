ARG IMAGE
FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS packages
COPY docker/swarm-node/versions.lock docker/swarm-node/requirements.lock scripts/swarm_v2/worker_image.py /opt/swarm-node/
RUN python -c 'import json; from pathlib import Path; s=json.loads(Path("/opt/swarm-node/versions.lock").read_text())["debian_snapshot"]; Path("/etc/apt/sources.list").write_text(f"deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/{s} bookworm main\ndeb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/{s} bookworm-security main\n")' \
    && rm /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends --download-only \
        $(python /opt/swarm-node/worker_image.py shell-packages --lock /opt/swarm-node/versions.lock --package openssh-server)

FROM ${IMAGE}
USER root
COPY --from=packages /var/cache/apt/archives/*.deb /packages/
COPY docker/swarm-node/versions.lock docker/swarm-node/requirements.lock docker/swarm-node/layout.json /opt/swarm-node/
COPY docker/swarm-node/ssh/ /opt/swarm-node/ssh/
COPY deploy/helm/agentihooks-swarm/ci/terminal-worker.sh /opt/swarm-node/terminal-worker.sh
RUN dpkg -i /packages/*.deb \
    && rm -r /packages \
    && rm -f /etc/ssh/ssh_host_* \
    && usermod -l worker -d /home/worker -m swarm \
    && groupmod -n worker swarm \
    && chmod 0755 /opt/swarm-node/ssh/start.sh /opt/swarm-node/terminal-worker.sh
USER 10001:10001
WORKDIR /home/worker
CMD ["/opt/swarm-node/terminal-worker.sh"]
