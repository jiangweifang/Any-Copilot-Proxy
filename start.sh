#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ "${1:-}" = "--headless" ]; then
    exec "$ROOT/.venv/bin/python" -m copilot_proxy.cli serve --root "$ROOT"
fi
exec "$ROOT/.venv/bin/python" -m copilot_proxy.desktop --root "$ROOT"
