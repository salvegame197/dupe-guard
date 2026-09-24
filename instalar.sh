#!/usr/bin/env bash
# Registra o guard no settings.json do Claude Code.
# Faz backup antes, funde em vez de sobrescrever, e se recusa a duplicar.
set -euo pipefail
AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
python3 - "$AQUI" <<'PYEOF'
import json, shutil, sys, time
from pathlib import Path

aqui = Path(sys.argv[1])
alvo = aqui / "guard.py"
cfg = Path.home() / ".claude" / "settings.json"

if not cfg.exists():
    sys.exit(f"nao achei {cfg} — o Claude Code esta instalado?")

dados = json.loads(cfg.read_text())
regras = dados.setdefault("hooks", {}).setdefault("PreToolUse", [])

# A trava que importa: se JA existe um qualidade-guard registrado (mesmo vindo
# de outro caminho), parar. Instalar por cima deixaria dois rodando na mesma
# edicao — dobro de latencia e aviso duplicado.
for r in regras:
    for h in r.get("hooks", []):
        cmd = h.get("command", "")
        if "qualidade-guard" in cmd or "guard.py" in cmd:
            print(f"ja existe um guard registrado:\n  {cmd}\n")
            print("nada foi alterado. remova o registro antigo antes, ou use")
            print("./desinstalar.sh se ele veio desta mesma pasta.")
            sys.exit(1)

bkp = cfg.with_suffix(f".json.bkp-{time.strftime('%Y%m%d-%H%M%S')}")
shutil.copy2(cfg, bkp)

regras.append({
    "matcher": "Write|Edit|MultiEdit",
    "hooks": [{"type": "command", "command": f"python3 {alvo}"}],
})
cfg.write_text(json.dumps(dados, indent=2, ensure_ascii=False) + "\n")

print(f"registrado: {alvo}")
print(f"backup:     {bkp}")
print("\nabra uma sessao nova do Claude Code para valer.")
PYEOF
