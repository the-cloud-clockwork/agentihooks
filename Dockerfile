FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS dependencies
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv
WORKDIR /build
COPY docker/swarm/requirements.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv venv /opt/venv && uv pip install --python /opt/venv/bin/python --compile-bytecode --require-hashes -r requirements.lock

FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /usr/local/bin/uv
WORKDIR /build
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    python -c 'import subprocess, tomllib; subprocess.run(["uv", "pip", "install", "--system", *tomllib.load(open("pyproject.toml", "rb"))["build-system"]["requires"]], check=True)'
COPY hooks/ hooks/
COPY scripts/ scripts/
COPY profiles/ profiles/
ARG VERSION=0.0.0
RUN --mount=type=cache,target=/root/.cache/uv \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS="$VERSION" uv build --wheel --no-build-isolation \
    && uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --prefix /opt/package --compile-bytecode --no-deps dist/*.whl

FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS herdr
ARG TARGETARCH=amd64
COPY docker/swarm-node/versions.lock docker/swarm-node/requirements.lock /opt/swarm-node/
COPY scripts/swarm_v2/worker_image.py /opt/swarm-node/worker_image.py
RUN python /opt/swarm-node/worker_image.py install --tool herdr --architecture "$TARGETARCH"

FROM mirror.gcr.io/library/python:3.12-slim-bookworm
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MALLOC_ARENA_MAX=2 \
    LEDGER_HOST=0.0.0.0 \
    LEDGER_PORT=8765 \
    LEDGER_DIR=/data \
    SWARM_RELOAD=0
RUN --mount=type=bind,source=docker/swarm-node,target=/opt/swarm-node \
    --mount=type=bind,source=scripts/swarm_v2/worker_image.py,target=/opt/worker_image.py \
    python -c 'import json; from pathlib import Path; s=json.loads(Path("/opt/swarm-node/versions.lock").read_text())["debian_snapshot"]; Path("/etc/apt/sources.list").write_text(f"deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/{s} bookworm main\ndeb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/{s} bookworm-security main\n")' \
    && rm /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends $(python /opt/worker_image.py shell-packages --package openssh-client) \
    && rm -r /var/lib/apt/lists/*
RUN groupadd --gid 10001 swarm && useradd --uid 10001 --gid swarm --create-home swarm && mkdir /data && chown swarm:swarm /data \
    && python -m compileall -q -x /site-packages/ /usr/local/lib/python3.12
COPY --from=dependencies /opt/venv /opt/venv
COPY --from=build /opt/package/ /opt/venv/
COPY --from=herdr /usr/local/bin/herdr /usr/local/bin/herdr
COPY media/agentihooks-logo.png /opt/venv/lib/python3.12/site-packages/media/agentihooks-logo.png
COPY docs/swarm-v2/schemas/ /opt/venv/lib/python3.12/site-packages/docs/swarm-v2/schemas/
COPY docs/swarm-v2/architecture.json /opt/venv/lib/python3.12/site-packages/docs/swarm-v2/architecture.json
USER 10001:10001
VOLUME /data
EXPOSE 8765
STOPSIGNAL SIGINT
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --start-interval=1s --retries=3 CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ["LEDGER_PORT"] + "/healthz", timeout=2).read()'
CMD ["python", "-c", "from scripts.swarm_ledger import run; run(['serve', '--serve'])"]
