#!/usr/bin/env bash
# Remove every hook pointing at this folder. Deletes no data.
set -euo pipefail
exec python3 "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/hooks_setup.py" uninstall
