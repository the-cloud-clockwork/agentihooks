#!/usr/bin/env bash
set -o errexit -o nounset -o pipefail

cleanup() {
  # ruleid: exit-trap-unguarded-command
  rm -r -- "$tls"
}
trap cleanup EXIT
