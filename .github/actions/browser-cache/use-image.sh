#!/usr/bin/env bash
set -euo pipefail

: "${ARC_PLAYWRIGHT_BROWSERS_PATH:?The queue runner image must supply Chromium}"
: "${PLAYWRIGHT_BROWSERS_PATH:?The queue browser path must be absolute}"
[[ "$PLAYWRIGHT_BROWSERS_PATH" == /* && -d "$ARC_PLAYWRIGHT_BROWSERS_PATH" ]] || {
  echo '::error::The queue runner image has no baked Chromium; rebuild the image.'
  exit 1
}
ln -sT "$ARC_PLAYWRIGHT_BROWSERS_PATH" "$PLAYWRIGHT_BROWSERS_PATH"
timeout --kill-after=5s 30s bash "$(dirname "$0")/verify.sh"
echo 'Using baked Chromium offline for queue tests'
