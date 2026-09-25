#!/usr/bin/env python3
"""
Hook SessionStart: inject a COMPACT INDEX of the vault into context.

An index, not the content: a real vault runs to thousands of pages and loading
it every session would be absurd. The index costs a few hundred tokens and
tells the model WHAT exists and HOW to search; the search itself (grep) takes
~0.01s and costs only the lines that match.

Sections come from the config ("context_sections"). Without it, every
top-level folder of the vault is listed.
"""
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import hook_output, labels, load_config                 # noqa: E402
from brain.session_log import ERRORS                                     # noqa: E402


def inventory(vault: Path):
    mds, total = [], 0
    for root, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not d.startswith("data-")]
        for f in files:
            if f.endswith(".md"):
                p = os.path.join(root, f)
                mds.append(p)
                try:
                    total += os.path.getsize(p)
                except OSError:
                    pass
    return mds, total


def listing(vault: Path, mds, subdir: str, depth: int, limit: int):
    """Page titles under a folder, relative to it."""
    base = os.path.join(str(vault), subdir)
    if not os.path.isdir(base):
        return []
    out = []
    for p in mds:
        if not p.startswith(base + os.sep):
            continue
        r = os.path.relpath(p, base)
        if r.count(os.sep) < depth:
            out.append(r[:-3])
    return sorted(out)[:limit]


def auto_sections(vault: Path, cfg):
    raw_top = Path(cfg["raw_dir"]).parts[0] if cfg["raw_dir"] else ""
    out = []
    for d in sorted(os.listdir(vault)):
        if d.startswith(".") or d.startswith("data-") or d == raw_top:
            continue
        if os.path.isdir(vault / d):
            out.append({"title": d, "path": d, "depth": 2, "limit": 30, "inline": False})
    return out


def main() -> int:
    try:
        sys.stdin.read()
    except Exception:
        pass
    cfg = load_config()
    vault = cfg["vault"]
    if not cfg["enabled"] or not vault.is_dir():
        return 0          # disabled, or the vault is not there (another machine)
    L = labels(cfg)
    mds, total = inventory(vault)

    lines = [L["ctx_header"], "",
             f"{L['ctx_path']}: {vault}",
             L["ctx_content"].format(n=len(mds), kb=total / 1024, ktok=total // 4 // 1000), "",
             L["ctx_dont_load"],
             f'  grep -ril "<term>" "{vault}" --include="*.md"',
             L["ctx_then_read"], ""]

    for sec in cfg["context_sections"] or auto_sections(vault, cfg):
        if sec.get("count_only"):
            base = os.path.join(str(vault), sec["path"])
            n = sum(1 for p in mds if p.startswith(base + os.sep))
            if n:
                lines += [f"## {sec['title']}", sec.get("note", "{count}").format(count=n), ""]
            continue
        pages = listing(vault, mds, sec["path"], sec.get("depth", 2), sec.get("limit", 30))
        if not pages:
            continue
        lines.append(f"## {sec['title']}")
        if sec.get("inline"):
            lines.append(", ".join(pages))
        else:
            lines += [f"- {p}" for p in pages]
        lines.append("")

    lines.append(L["ctx_when"])
    lines += [l.replace("{note_language}", cfg["note_language"]) for l in L["ctx_when_lines"]]

    # session_log writes here when it fails. Saying so in the next session is
    # what keeps the leak from coming back silently.
    if ERRORS.exists() and time.time() - ERRORS.stat().st_mtime < 7 * 86400:
        lines += ["", L["ctx_failed_title"], L["ctx_failed_body"].format(path=ERRORS)]

    print(hook_output("SessionStart", "\n".join(lines)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
