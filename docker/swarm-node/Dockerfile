ARG BASE_IMAGE=python:3.12.12-slim-bookworm@sha256:2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301
FROM ${BASE_IMAGE}
ARG BASE_IMAGE
ARG TARGETARCH
ARG SOURCE_REVISION=unknown
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISABLE_AUTOUPDATER=1 \
    HOME=/home/worker
WORKDIR /build
COPY docker/swarm-node/versions.lock docker/swarm-node/requirements.lock /opt/swarm-node/
COPY scripts/swarm_v2/worker_image.py /opt/swarm-node/worker_image.py
COPY docker/swarm-node/supervisor.py docker/swarm-node/entrypoint.sh /opt/swarm-node/
RUN python /opt/swarm-node/worker_image.py validate --architecture "$TARGETARCH" --base-image "$BASE_IMAGE" \
    && python -c 'import json; from pathlib import Path; s=json.loads(Path("/opt/swarm-node/versions.lock").read_text())["debian_snapshot"]; Path("/etc/apt/sources.list").write_text(f"deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/{s} bookworm main\ndeb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/{s} bookworm-security main\n")' \
    && rm /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends $(python /opt/swarm-node/worker_image.py shell-packages) \
    && rm -r /var/lib/apt/lists/*
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /opt/swarm-node/requirements.lock \
    && python /opt/swarm-node/worker_image.py install --architecture "$TARGETARCH"
COPY pyproject.toml README.md ./
COPY hooks/ hooks/
COPY scripts/ scripts/
COPY profiles/ profiles/
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS=0.0.0 pip install --no-cache-dir --no-deps --no-build-isolation .
COPY profiles/ /opt/agentihooks/templates/
RUN python /opt/swarm-node/worker_image.py manifest --architecture "$TARGETARCH" --source-revision "$SOURCE_REVISION" \
    && chmod -R a-w /opt/agentihooks/templates /opt/swarm-node /opt/venv \
    && chmod a+x /opt/swarm-node/entrypoint.sh \
    && groupadd --gid 10001 worker \
    && useradd --uid 10001 --gid worker --create-home worker \
    && chown worker:worker /home/worker \
    && rm -r /build
USER 10001:10001
WORKDIR /home/worker
ENTRYPOINT ["/opt/swarm-node/entrypoint.sh"]
CMD ["python", "/opt/swarm-node/worker_image.py", "report"]
