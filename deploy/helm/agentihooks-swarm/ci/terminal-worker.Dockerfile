ARG IMAGE
FROM ${IMAGE}
USER root
COPY docker/swarm-node/versions.lock docker/swarm-node/requirements.lock docker/swarm-node/layout.json /opt/swarm-node/
COPY docker/swarm-node/ssh/ /opt/swarm-node/ssh/
COPY deploy/helm/agentihooks-swarm/ci/terminal-worker.sh /opt/swarm-node/terminal-worker.sh
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        $(python -m scripts.swarm_v2.worker_image shell-packages --lock /opt/swarm-node/versions.lock --package openssh-server) \
    && rm -r /var/lib/apt/lists/* \
    && rm -f /etc/ssh/ssh_host_* \
    && usermod -l worker -d /home/worker -m swarm \
    && groupmod -n worker swarm \
    && chmod 0755 /opt/swarm-node/ssh/start.sh /opt/swarm-node/terminal-worker.sh
USER 10001:10001
WORKDIR /home/worker
CMD ["/opt/swarm-node/terminal-worker.sh"]
