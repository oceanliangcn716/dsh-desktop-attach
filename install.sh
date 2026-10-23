#!/bin/bash
set -euo pipefail
APP="$(cd "$(dirname "$0")" && pwd)"
exec "${PYTHON3:-python3}" "$APP/tools/manage.py" install "$@"
