ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
USER 0:0
RUN chown 10001:10001 /home/worker
USER 10001:10001
COPY tests/integration/swarm_node/process_fixture.py tests/integration/swarm_node/container_case.py tests/integration/swarm_node/compatibility_case.py /opt/fixture/
COPY docker/swarm-node/fixtures/profiles /opt/fixture/profiles
CMD ["python", "/opt/fixture/container_case.py", "complete"]
