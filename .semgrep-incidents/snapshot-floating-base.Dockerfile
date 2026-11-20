# ruleid: snapshot-floating-base
FROM mirror.gcr.io/library/python:3.12-slim-bookworm AS packages
COPY docker/swarm-node/versions.lock /opt/swarm-node/
RUN python -c 'print("debian_snapshot")'

# ok: snapshot-floating-base
FROM mirror.gcr.io/library/python:3.12.12-slim-bookworm AS pinned
RUN echo https://snapshot.debian.org/archive/debian/20260301T000000Z

# ok: snapshot-floating-base
FROM python:3.12-slim-bookworm AS independent
RUN python --version

# ok: snapshot-floating-base
FROM python:3.12.12-slim-bookworm AS separate_snapshot
RUN echo https://snapshot.debian.org/archive/debian/20260301T000000Z

# ruleid: snapshot-floating-base
FROM --platform=linux/amd64 python:latest AS latest
RUN echo https://snapshot.debian.org/archive/debian/20260301T000000Z

# ruleid: snapshot-floating-base
FROM debian:bookworm AS debian
RUN echo https://snapshot.debian.org/archive/debian/20260301T000000Z

# ok: snapshot-floating-base
FROM python:3.12-slim-bookworm@sha256:2986c55feb36e6cae00fa1fefb454283e4b33f35e75ff8bdd123b134130be301 AS digest
RUN echo https://snapshot.debian.org/archive/debian/20260301T000000Z

# ruleid: snapshot-floating-base
ARG FLOATING=python:3.12-slim-bookworm
FROM ${FLOATING} AS argument
RUN echo https://snapshot.debian.org/archive/debian/20260301T000000Z

# ok: snapshot-floating-base
ARG LOCKED=python:3.12.12-slim-bookworm
FROM ${LOCKED} AS locked_argument
RUN echo https://snapshot.debian.org/archive/debian/20260301T000000Z
