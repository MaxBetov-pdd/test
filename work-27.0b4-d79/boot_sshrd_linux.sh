#!/usr/bin/env bash
set -Eeuo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$DIR/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
HOST_LOG="$LOG_DIR/sshrd-$STAMP.log"

mkdir -p "$LOG_DIR"

echo "Artifact:  $DIR"
echo "Host log: $HOST_LOG"
echo "Expected: iPhone SE (2nd gen), d79ap, A13"

if ! command -v irecovery >/dev/null 2>&1; then
  echo "error: irecovery is not on PATH" >&2
  exit 1
fi

sudo -v
set +e
sudo -E python3 "$DIR/boot_sshrd_linux.py" --bootchain "$DIR" "$@" \
  2>&1 | tee "$HOST_LOG"
status="${PIPESTATUS[0]}"
set -e

ln -sfn "$(basename "$HOST_LOG")" "$LOG_DIR/latest-sshrd.log"

if (( status != 0 )); then
  echo "Boot failed with status $status. Full log: $HOST_LOG" >&2
  exit "$status"
fi

echo "Boot completed. Full log: $HOST_LOG"
