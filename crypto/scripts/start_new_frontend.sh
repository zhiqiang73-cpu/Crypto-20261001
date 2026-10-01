#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
printf '%s\n' 'New BTC Quant Console: http://127.0.0.1:8788/'
exec python3 -m http.server 8788 --bind 127.0.0.1 --directory frontend
