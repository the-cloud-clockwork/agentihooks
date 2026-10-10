# syntax=mirror.gcr.io/docker/dockerfile:1
FROM mirror.gcr.io/library/python:3.12-slim AS build
WORKDIR /build
COPY pyproject.toml README.md ./
RUN python -c 'import tomllib; print("\n".join(tomllib.load(open("pyproject.toml", "rb"))["project"]["optional-dependencies"]["ledger"]))' > ledger-requirements.txt
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir -r ledger-requirements.txt
COPY hooks/ hooks/
COPY scripts/ scripts/
COPY profiles/ profiles/
ARG VERSION=0.0.0
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS="$VERSION" /opt/venv/bin/pip install --no-cache-dir --no-deps .

FROM mirror.gcr.io/library/python:3.12-slim
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LEDGER_HOST=0.0.0.0 \
    LEDGER_PORT=8765 \
    LEDGER_DIR=/data \
    SWARM_RELOAD=0
RUN groupadd --gid 10001 swarm && useradd --uid 10001 --gid swarm --create-home swarm && mkdir /data && chown swarm:swarm /data
COPY --from=build /opt/venv /opt/venv
COPY media/agentihooks-logo.png /opt/venv/lib/python3.12/site-packages/media/agentihooks-logo.png
USER 10001:10001
VOLUME /data
EXPOSE 8765
STOPSIGNAL SIGINT
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:" + os.environ["LEDGER_PORT"] + "/healthz", timeout=2).read()'
CMD ["python", "-c", "from scripts.swarm_ledger import run; run(['serve', '--serve'])"]
