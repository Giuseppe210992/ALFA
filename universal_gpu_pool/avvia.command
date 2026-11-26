#!/usr/bin/env bash
# macOS: doppio clic su questo file per avviare la webapp
cd "$(dirname "$0")" || exit 1
exec ./avvia.sh "$@"
