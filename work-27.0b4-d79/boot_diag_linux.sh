#!/usr/bin/env bash
set -Eeuo pipefail

DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$DIR/logs"
STAMP="$(date +%Y%m%d-%H%M%S)"
HOST_LOG="$LOG_DIR/diagnostic-$STAMP.log"

mkdir -p "$LOG_DIR"

echo "Artifact:  $DIR"
echo "Host log: $HOST_LOG"
echo "Expected: iPhone SE (2nd gen), d79ap, A13"
echo "Mode:     panic-preserving diagnostic normal boot (NO RESTORE)"
echo "Watch the phone and photograph the final readable verbose/panic lines."

sudo -v
set +e
sudo -E python3 "$DIR/boot_normal_linux.py" --bootchain "$DIR" \
  --artifact-mode normal-diagnostic "$@" 2>&1 | tee "$HOST_LOG"
status="${PIPESTATUS[0]}"
set -e

ln -sfn "$(basename "$HOST_LOG")" "$LOG_DIR/latest-diagnostic.log"

if (( status != 0 )); then
  echo "Diagnostic boot failed with status $status. Full log: $HOST_LOG" >&2
  exit "$status"
fi

echo "Diagnostic observation completed. Full host log: $HOST_LOG"
