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
rules = data.get("hooks", {}).get("PreToolUse", [])

before = sum(len(r.get("hooks", [])) for r in rules)
kept = []
for r in rules:
    hs = [h for h in r.get("hooks", []) if here not in h.get("command", "")]
    if hs:
        r["hooks"] = hs
        kept.append(r)

if sum(len(r["hooks"]) for r in kept) == before:
    sys.exit("no entry from this folder found - nothing to do.")

bkp = cfg.with_suffix(f".json.bkp-{time.strftime('%Y%m%d-%H%M%S')}")
shutil.copy2(cfg, bkp)
data["hooks"]["PreToolUse"] = kept
cfg.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
print(f"removed. backup at {bkp}")
PYEOF
