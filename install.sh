#!/usr/bin/env bash
# Register the guard and the second brain in Claude Code's settings.json.
# Safe to run again after `git pull`: it syncs, it does not duplicate.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/hooks_setup.py" install
