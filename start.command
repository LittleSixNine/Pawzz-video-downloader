#!/bin/bash
set -euo pipefail
exec bash "$(dirname "$0")/platforms/desktop/start.command" "$@"
