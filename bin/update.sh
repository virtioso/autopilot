#! /bin/sh

set -euo pipefail

cat "$1/update.tar" | ssh root@192.168.101.112 'tar -C / -xvf - && reboot'
