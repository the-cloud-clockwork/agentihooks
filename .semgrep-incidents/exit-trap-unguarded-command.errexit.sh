#!/usr/bin/env bash
set -o nounset -o pipefail -o errexit

cleanup() {
  # ruleid: exit-trap-unguarded-command
  rm -r -- "$tls"
}
trap cleanup EXIT
