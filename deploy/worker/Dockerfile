ARG WORKER_IMAGE=ghcr.io/the-cloud-clockwork/agentihooks-worker:dev
ARG AGENTIHOOKS_SOURCE=git
FROM mirror.gcr.io/library/node:22.20.0-bookworm-slim AS node
FROM ghcr.io/astral-sh/uv:0.10.9 AS uv
FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS source-git
ARG AGENTIHOOKS_REF=dev
ADD https://github.com/the-cloud-clockwork/agentihooks.git#${AGENTIHOOKS_REF} /build

FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS source-local
COPY pyproject.toml README.md /build/
COPY hooks /build/hooks
COPY scripts /build/scripts
COPY profiles /build/profiles

FROM source-${AGENTIHOOKS_SOURCE} AS wheel
ARG AGENTIHOOKS_VERSION=0.0.0
WORKDIR /build
RUN SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS="$AGENTIHOOKS_VERSION" pip wheel --no-deps --wheel-dir /wheels . \
    && python -c "import tomllib; dev = tomllib.load(open('pyproject.toml', 'rb'))['project']['optional-dependencies']['dev']; print(*[r for r in dev if r.startswith('ruff==')])" > /wheels/tools.txt

FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS gh
RUN apt-get update && apt-get install -y --no-install-recommends gh

FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS browser-libs
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
RUN dpkg-query -W -f='${Package}\n' | sort > /base-packages \
    && apt-get update \
    && apt-get install -y --no-install-recommends libasound2 libatk-bridge2.0-0 libatk1.0-0 libatspi2.0-0 libcairo2 libcups2 \
        libdbus-1-3 libdrm2 libgbm1 libglib2.0-0 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
        libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 fonts-liberation \
    && mkdir /browser-libs \
    && dpkg-query -W -f='${Package}\n' | sort | comm -13 /base-packages - | xargs dpkg -L \
        | while read -r path; do if [ ! -d "$path" ] && { [ -f "$path" ] || [ -L "$path" ]; }; then \
            printf '%s/%s\n' "$(readlink -f "$(dirname "$path")")" "$(basename "$path")"; fi; done \
        | sort -u | tar -cf - --no-recursion -T - | tar -xf - -C /browser-libs

FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS overlay
COPY --from=wheel /wheels /wheels
WORKDIR /opt/venv
RUN python -m venv /opt/venv \
    && bin/pip install --no-cache-dir --no-deps /wheels/*.whl -r /wheels/tools.txt \
    && rm -rf bin/python* bin/pip* bin/activate* bin/Activate.ps1 pyvenv.cfg include lib64 lib/python3.12/site-packages/pip lib/python3.12/site-packages/pip-*.dist-info \
    && helper=lib/python3.12/site-packages/profiles/package/skills/worktree/scripts/wt.sh \
    && chmod 0755 "$helper" \
    && mkdir /links \
    && ln -s "/opt/venv/$helper" /links/wt.sh \
    && ln -s ../lib/node_modules/npm/bin/npm-cli.js /links/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /links/npx

FROM ${WORKER_IMAGE}
COPY --link=true --from=browser-libs /browser-libs/ /
COPY --link=true --from=node /usr/local/bin/node /usr/local/bin/node
COPY --link=true --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY --link=true --from=uv /uv /uvx /usr/local/bin/
COPY --link=true --from=gh /usr/bin/gh /usr/bin/gh
COPY --link=true --from=overlay /links/ /usr/local/bin/
COPY --link=true --from=overlay /opt/venv/ /opt/venv/
ENV NPM_CONFIG_PREFIX=/home/worker/.local \
    PATH=/home/worker/.local/bin:$PATH
COPY --link=true --chmod=0755 deploy/worker/start.sh /opt/worker-hive/start.sh
COPY --link=true --chmod=0755 deploy/worker/gh /usr/local/bin/gh
USER 10001:10001
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/worker-hive/start.sh"]
CMD []
