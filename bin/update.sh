#! /bin/sh

set -euo pipefail

TARGET_USER="${AUTOPILOT_TARGET_USER:-root}"
TARGET_IP="${AUTOPILOT_TARGET_IP:-192.168.101.112}"

cat "$1/update.tar" | ssh "${TARGET_USER}@${TARGET_IP}" 'tar -C / -xvf - && reboot'
