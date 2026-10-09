ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
COPY tests/integration/swarm_node/process_fixture.py tests/integration/swarm_node/container_case.py /opt/fixture/
COPY docker/swarm-node/fixtures/profiles /opt/fixture/profiles
CMD ["python", "/opt/fixture/container_case.py", "complete"]
