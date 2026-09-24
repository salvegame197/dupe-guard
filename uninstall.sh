#!/usr/bin/env bash
# Removes ONLY the entry pointing at this folder. Touches nothing else.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$HERE" <<'PYEOF'
import json, shutil, sys, time
from pathlib import Path

here = str((Path(sys.argv[1]) / "guard.py").resolve())
cfg = Path.home() / ".claude" / "settings.json"
data = json.loads(cfg.read_text())
removed = 0
for event in ("PreToolUse", "PostToolUse"):
    rules = data.get("hooks", {}).get(event, [])
    kept = []
    for r in rules:
        hs = [h for h in r.get("hooks", []) if here not in h.get("command", "")]
        removed += len(r.get("hooks", [])) - len(hs)
        if hs:
            r["hooks"] = hs
            kept.append(r)
    if event in data.get("hooks", {}):
        data["hooks"][event] = kept

if not removed:
    sys.exit("no entry from this folder found - nothing to do.")

bkp = cfg.with_suffix(f".json.bkp-{time.strftime('%Y%m%d-%H%M%S')}")
shutil.copy2(cfg, bkp)
cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
print(f"removed. backup at {bkp}")
PYEOF
