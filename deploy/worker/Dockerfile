# syntax=docker/dockerfile:1
ARG WORKER_IMAGE=ghcr.io/the-cloud-clockwork/agentihooks-worker:dev
FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS wheel
WORKDIR /build
COPY pyproject.toml README.md ./
COPY hooks/ hooks/
COPY scripts/ scripts/
COPY profiles/ profiles/
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS=0.0.0 pip wheel --no-deps --wheel-dir /wheels .

FROM ${WORKER_IMAGE}
USER root
COPY --from=wheel /wheels /wheels
RUN pip install --no-cache-dir --no-deps --force-reinstall /wheels/*.whl \
    && rm -r /wheels \
    && python -c 'import profiles; from pathlib import Path; import os; os.symlink(Path(profiles.__file__).parent / "package/skills/worktree/scripts/wt.sh", "/usr/local/bin/wt.sh")'
COPY --chmod=0755 deploy/worker/start.sh /opt/worker-hive/start.sh
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/worker-hive/start.sh"]
CMD []
