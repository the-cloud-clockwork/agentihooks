ARG WORKER_IMAGE=ghcr.io/the-cloud-clockwork/agentihooks-worker:dev
FROM mirror.gcr.io/library/node:22.20.0-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS wheel
WORKDIR /build
COPY pyproject.toml README.md ./
COPY hooks/ hooks/
COPY scripts/ scripts/
COPY profiles/ profiles/
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS=0.0.0 pip wheel --no-deps --wheel-dir /wheels .

FROM ${WORKER_IMAGE}
USER root
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --from=uv /uv /uvx /usr/local/bin/
RUN apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -r /var/lib/apt/lists/* \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx
COPY --from=wheel /wheels /wheels
RUN pip install --no-cache-dir --no-deps --force-reinstall /wheels/*.whl \
    && rm -r /wheels \
    && python -c 'import profiles; from pathlib import Path; import os; helper = Path(profiles.__file__).parent / "package/skills/worktree/scripts/wt.sh"; helper.chmod(0o755); os.symlink(helper, "/usr/local/bin/wt.sh")' \
    && usermod -s /bin/bash worker
ENV NPM_CONFIG_PREFIX=/home/worker/.local \
    PATH=/home/worker/.local/bin:$PATH
COPY --chmod=0755 deploy/worker/start.sh /opt/worker-hive/start.sh
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/worker-hive/start.sh"]
CMD []
