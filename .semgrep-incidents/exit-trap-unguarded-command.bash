#!/usr/bin/env bash
set -uo pipefail

cleanup() {
  rm -r -- "$tls"
}
trap cleanup EXIT
trap 'docker compose down --volumes' EXIT
