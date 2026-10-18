ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
USER 0:0
RUN python -c "import secrets, string; c = string.ascii_letters + string.digits; print('ghs_' + ''.join(secrets.choice(c) for _ in range(36)))" > /opt/agentihooks/.build-token
USER 10001:10001
