#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"

if ! command -v apt-get >/dev/null; then
    echo "This installer currently supports Debian/Ubuntu systems." >&2
    exit 1
fi

sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    ca-certificates curl git jq openssh-client sshpass \
    python3 python3-pip python3-venv \
    libusb-1.0-0 usbutils usbmuxd libusbmuxd-tools libimobiledevice-utils

python3 -m venv "$ROOT/.venv-linux"
"$ROOT/.venv-linux/bin/python" -m pip install --upgrade pip
"$ROOT/.venv-linux/bin/python" -m pip install -r "$ROOT/requirements-linux.txt"

chmod 0755 "$SCRIPT_DIR/ich" "$SCRIPT_DIR/setup.sh"
sudo install -m 0644 "$SCRIPT_DIR/39-ich-apple.rules" /etc/udev/rules.d/39-ich-apple.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo ln -sfn "$SCRIPT_DIR/ich" /usr/local/bin/ich

echo "Linux host setup complete. Reconnect the phone, then run: ich doctor"
