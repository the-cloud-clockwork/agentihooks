ARG WORKER_IMAGE
FROM ${WORKER_IMAGE}
USER 0:0
RUN mv /usr/local/bin/herdr /usr/local/bin/herdr-accepted
COPY --chmod=0755 herdr /usr/local/bin/herdr
USER 10001:10001
