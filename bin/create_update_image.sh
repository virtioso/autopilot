#! /usr/bin/env bash

set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <kernel image> <destination dir>" >&2
    exit 1
fi

SRC="$1"
DEST="$2"

if [ ! -f "$SRC" ]; then
    echo "Error: file $SRC does not exist" >&2
    exit 1
fi

TMPDIR="$(mktemp -d)"

trap 'rm -rf "$TMPDIR"' EXIT

mkdir -p "$TMPDIR/boot"
cp -- "$SRC" "$TMPDIR/boot/Image-6.17-tegra"

mkdir -p "$DEST"

tar -C "$TMPDIR" -cpf "$DEST/update.tar" .
