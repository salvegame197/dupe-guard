#!/usr/bin/env python3
"""
Register dupe-guard's hooks in Claude Code's settings.json without the plugin
system — the guard and the second brain, exactly as the plugin would.

hooks/hooks.json is the only definition: this script reads it and rewrites
${CLAUDE_PLUGIN_ROOT} to this folder and `python3` to the interpreter running
it (Claude Code may launch hooks with a reduced PATH where a bare python3 is a
system stub). Two copies of the list would drift, and they did.

`install` is a sync: every entry pointing at this folder is replaced by the
current set, so after `git pull` running it again picks up new or changed
hooks without duplicating. It refuses when a guard from ANOTHER path is
registered, or when the dupe-guard plugin is enabled: either way the guard
would run twice on every edit.

`uninstall` removes every entry pointing at this folder and nothing else. It
deletes no data: config, conventions and the vault stay where they are.

Usage:
    hooks_setup.py install
    hooks_setup.py uninstall
"""
import json
import os
import shutil
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = Path.home() / ".claude" / "settings.json"
HOME_DIR = Path(os.environ.get("DUPE_GUARD_HOME") or (Path.home() / ".dupe-guard"))


def wanted():
    """{event: [rule]} from hooks.json, resolved to this folder."""
    spec = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    out = {}
    for event, rules in spec.items():
        for rule in rules:
            hooks = []
            for h in rule["hooks"]:
                cmd = h["command"].replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
                if cmd.startswith("python3 "):
                    cmd = f'"{sys.executable}" ' + cmd[len("python3 "):]
                hooks.append({**h, "command": cmd})
            new = {k: v for k, v in rule.items() if k != "hooks"}
            new["hooks"] = hooks
            out.setdefault(event, []).append(new)
    return out


def ours(cmd: str) -> bool:
    return str(ROOT) in cmd


def strip_ours(hooks: dict):
    """Settings hooks without any entry pointing at this folder; count removed."""
    removed, out = 0, {}
    for event, rules in hooks.items():
        kept = []
        for r in rules:
            hs = [h for h in r.get("hooks", []) if not ours(h.get("command", ""))]
            removed += len(r.get("hooks", [])) - len(hs)
            if hs:
                kept.append({**r, "hooks": hs})
        if kept:
            out[event] = kept
    return out, removed


def conflicts(data: dict):
    """Reasons not to install: a foreign guard, or the plugin itself."""
    why = []
    for key, on in (data.get("enabledPlugins") or {}).items():
        if on and key.split("@")[0] == "dupe-guard":
            why.append(f"the dupe-guard plugin is enabled ({key}); uninstall it or use it instead")
    for rules in (data.get("hooks") or {}).values():
        for r in rules:
            for h in r.get("hooks", []):
                cmd = h.get("command", "")
                if ("guard.py" in cmd or "dupe-guard" in cmd) and not ours(cmd):
                    why.append(f"a guard from another path is registered:\n    {cmd}")
    return list(dict.fromkeys(why))       # same guard on two events: say it once


def write(data: dict):
    bkp = SETTINGS.with_suffix(f".json.bkp-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copy2(SETTINGS, bkp)
    SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return bkp


def brain_status():
    try:
        cfg = (json.loads((HOME_DIR / "config.json").read_text()) or {}).get("brain") or {}
    except (OSError, ValueError):
        cfg = {}
    if cfg.get("enabled"):
        return f"second brain: ENABLED, vault {cfg.get('vault', '(default)')}"
    return (f"second brain: registered but off. To turn it on, ask Claude to run the "
            f"memory skill, or set \"brain\": {{\"enabled\": true}} in {HOME_DIR / 'config.json'}")


def install() -> int:
    if not SETTINGS.exists():
        print(f"{SETTINGS} not found — is Claude Code installed?")
        return 1
    data = json.loads(SETTINGS.read_text())
    why = conflicts(data)
    if why:
        print("nothing was changed:\n  - " + "\n  - ".join(why))
        return 1
    hooks, removed = strip_ours(data.get("hooks") or {})
    add = wanted()
    for event, rules in add.items():
        hooks.setdefault(event, []).extend(rules)
    new = {**data, "hooks": hooks}
    n = sum(len(r["hooks"]) for rules in add.values() for r in rules)
    if new == data:
        print(f"already up to date: {n} hooks from {ROOT}")
        print(brain_status())
        return 0
    bkp = write(new)
    print(("updated" if removed else "installed") + f": {n} hooks from {ROOT}"
          + (f" (replaced {removed})" if removed else ""))
    print(f"backup: {bkp}")
    print(brain_status())
    print("\nopen a new Claude Code session for it to take effect.")
    return 0


def uninstall() -> int:
    if not SETTINGS.exists():
        print(f"{SETTINGS} not found — nothing to do.")
        return 0
    data = json.loads(SETTINGS.read_text())
    hooks, removed = strip_ours(data.get("hooks") or {})
    if not removed:
        print(f"no hook from {ROOT} is registered — nothing to do.")
        return 0
    bkp = write({**data, "hooks": hooks})
    print(f"removed {removed} hooks. backup: {bkp}")
    print(f"\nnothing was deleted from disk. What remains, if you want it gone:")
    print(f"  {HOME_DIR}   config, conventions, search index, state (safe to delete)")
    try:
        vault = (json.loads((HOME_DIR / "config.json").read_text()) or {}).get("brain", {}).get("vault")
    except (OSError, ValueError, AttributeError):
        vault = None
    if vault:
        print(f"  {vault}   your notes — keep them")
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd not in ("install", "uninstall"):
        print(__doc__)
        sys.exit(2)
    sys.exit(install() if cmd == "install" else uninstall())
