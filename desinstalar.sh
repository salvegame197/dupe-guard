#!/usr/bin/env bash
# Remove SOMENTE o registro que aponta para esta pasta. Nao toca em mais nada.
set -euo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$AQUI" <<'PYEOF'
import json, shutil, sys, time
from pathlib import Path

aqui = str((Path(sys.argv[1]) / "guard.py").resolve())
cfg = Path.home() / ".claude" / "settings.json"
dados = json.loads(cfg.read_text())
regras = dados.get("hooks", {}).get("PreToolUse", [])

antes = len(regras)
novas = []
for r in regras:
    hs = [h for h in r.get("hooks", []) if aqui not in h.get("command", "")]
    if hs:
        r["hooks"] = hs
        novas.append(r)

if len(novas) == antes and all(len(r.get("hooks", [])) for r in novas):
    sys.exit("nenhum registro desta pasta encontrado — nada a fazer.")

bkp = cfg.with_suffix(f".json.bkp-{time.strftime('%Y%m%d-%H%M%S')}")
shutil.copy2(cfg, bkp)
dados["hooks"]["PreToolUse"] = novas
cfg.write_text(json.dumps(dados, indent=2, ensure_ascii=False) + "\n")
print(f"removido. backup em {bkp}")
PYEOF
