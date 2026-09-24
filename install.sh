#!/usr/bin/env bash
# Register the guard in Claude Code's settings.json.
# Backs the file up first, merges instead of overwriting, refuses to duplicate.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$HERE" <<'PYEOF'
import json, shutil, sys, time
from pathlib import Path

here = Path(sys.argv[1])
target = here / "guard.py"
cfg = Path.home() / ".claude" / "settings.json"

if not cfg.exists():
    sys.exit(f"{cfg} not found - is Claude Code installed?")

data = json.loads(cfg.read_text())
rules = data.setdefault("hooks", {}).setdefault("PreToolUse", [])

# The check that matters: if a guard is ALREADY registered (from any path),
# stop. Installing over it would leave two running on the same edit - double
# the latency and a duplicated warning.
for r in rules:
    for h in r.get("hooks", []):
        cmd = h.get("command", "")
        if "guard.py" in cmd or "dupe-guard" in cmd:
            print(f"a guard is already registered:\n  {cmd}\n")
            print("nothing was changed. remove the old entry first, or run")
            print("./uninstall.sh if it came from this same folder.")
            sys.exit(1)

bkp = cfg.with_suffix(f".json.bkp-{time.strftime('%Y%m%d-%H%M%S')}")
shutil.copy2(cfg, bkp)

rules.append({
    "matcher": "Write|Edit|MultiEdit",
    "hooks": [{"type": "command", "command": f"python3 {target}"}],
})
cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")

print(f"registered: {target}")
print(f"backup:     {bkp}")
print("\nopen a new Claude Code session for it to take effect.")
PYEOF
