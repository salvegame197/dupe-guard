#!/usr/bin/env python3
"""
Hook SessionStart: show the harness proposals waiting for a decision.

Short on purpose: the count and the five most recent. The whole page is in the
vault. Nothing pending, nothing said.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import hook_output, load_config                          # noqa: E402


def main():
    try:
        sys.stdin.read()
    except Exception:
        pass
    cfg = load_config()
    if not cfg["enabled"]:
        return 0
    from brain import proposals
    p = proposals.pending(cfg)
    if not p:
        return 0
    lines = [f"## Harness proposals waiting for a decision: {len(p)}", ""]
    for it in p[-5:]:
        lines.append(f"- `{it['id']}` **{it['kind']}** `{it['target']}` — {it['text'][:110]}")
    if len(p) > 5:
        lines.append(f"- _(+{len(p) - 5} more on the page)_")
    page = Path(cfg["proposals_page"]).stem
    lines += ["", f"_The user decides, in chat or by marking `[x]`/`[-]` in [[{page}]]; "
                  f"then run `python3 {Path(__file__).parent / 'proposals.py'} --apply`. "
                  f"Do not apply anything on your own._"]
    print(hook_output("SessionStart", "\n".join(lines)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
