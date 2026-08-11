#!/usr/bin/env bash
set -Eeuo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$DIR/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
HOST_LOG="$LOG_DIR/normal-$STAMP.log"

mkdir -p "$LOG_DIR"

echo "Artifact:  $DIR"
echo "Host log: $HOST_LOG"
echo "Expected: iPhone SE (2nd gen), d79ap, A13"
echo "Mode:     experimental tethered normal boot (NO RESTORE)"

sudo -v
set +e
sudo -E python3 "$DIR/boot_normal_linux.py" --bootchain "$DIR" "$@" \
  2>&1 | tee "$HOST_LOG"
status="${PIPESTATUS[0]}"
set -e

ln -sfn "$(basename "$HOST_LOG")" "$LOG_DIR/latest-normal.log"

if (( status != 0 )); then
  echo "Normal boot failed with status $status. Full log: $HOST_LOG" >&2
  exit "$status"
fi

echo "Normal-mode USB appeared. Full log: $HOST_LOG"
