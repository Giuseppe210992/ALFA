#!/usr/bin/env bash
# Linux/macOS: avvia la webapp raggiungibile dal telefono (stessa Wi-Fi)
cd "$(dirname "$0")" || exit 1
exec ./avvia.sh --lan "$@"
