FROM ghcr.io/the-cloud-clockwork/arc-runner:dev

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap \
    && SUDO_FORCE_REMOVE=yes apt-get purge -y sudo \
    && gpasswd -d runner sudo \
    && rm -rf /etc/sudoers /etc/sudoers.d /var/lib/apt/lists/*

USER runner
RUN --mount=type=bind,source=.,target=/mnt/agentihooks \
    set -e; \
    cp -r /mnt/agentihooks /tmp/agentihooks; \
    mkdir /tmp/agentihooks-wheel; \
    cd /tmp/agentihooks; \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS=0.0.0 /opt/agentihooks-venv/bin/python \
      -c 'import sys; from setuptools import build_meta; build_meta.build_wheel(sys.argv[1])' /tmp/agentihooks-wheel; \
    /home/runner/.local/bin/uv pip install --no-cache --python /opt/agentihooks-venv/bin/python \
      --find-links /opt/wheelhouse --constraint .github/test-requirements.lock --excludes .github/test-excludes.txt \
      "agentihooks[dev,all] @ file://$(ls /tmp/agentihooks-wheel/agentihooks-*.whl)"; \
    sha256sum .github/test-requirements.lock | cut -d ' ' -f 1 > /opt/agentihooks-venv/lockfile.sha256; \
    rm -rf /tmp/agentihooks /tmp/agentihooks-wheel

ENV PATH=/opt/agentihooks-venv/bin:$PATH
ENTRYPOINT ["agentihooks", "ci-lab-worker"]
