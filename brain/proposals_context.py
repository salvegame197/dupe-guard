#!/usr/bin/env python3
"""
Hook SessionStart: the morning report of the unattended distiller, and the
harness proposals waiting for a decision.

Short on purpose: the count and the five most recent. The whole page is in the
vault. Nothing pending, nothing said.
"""
import json
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
    lines = []

    # The morning report: what the unattended distiller wrote since the last
    # session. It replaces the gate that stopped after the first note: review
    # after, with one command to undo.
    fresh = unseen_report()
    if fresh:
        written = [e for e in fresh if e.get("kind") == "written"]
        others = [e for e in fresh if e.get("kind") in ("error", "lint")]
        lines += [f"## Distilled since your last session: {len(written)} note(s)", ""]
        for e in written[-8:]:
            undo = f" — undo: `git -C \"{cfg['vault']}\" revert {e['commit']}`" if e.get("commit") else ""
            sup = f" ({e['superseded']} section(s) marked superseded)" if e.get("superseded") else ""
            lines.append(f"- **{e['note']}** ({e['day']}): {e.get('title') or '(untitled)'}{sup}{undo}")
        for e in others[-3:]:
            lines.append(f"- ⚠️ {e['kind']}: {e.get('note', '')} {e.get('why') or '; '.join(e.get('problems', []))[:160]}")
        lines.append("")

    p = proposals.pending(cfg)
    if p:
        lines += [f"## Harness proposals waiting for a decision: {len(p)}", ""]
        for it in p[-5:]:
            lines.append(f"- `{it['id']}` **{it['kind']}** `{it['target']}` — {it['text'][:110]}")
        if len(p) > 5:
            lines.append(f"- _(+{len(p) - 5} more on the page)_")
        page = Path(cfg["proposals_page"]).stem
        lines += ["", f"_The user decides, in chat or by marking `[x]`/`[-]` in [[{page}]]; "
                      f"then run `python3 {Path(__file__).parent / 'proposals.py'} --apply`. "
                      f"Do not apply anything on your own._"]
    if not lines:
        return 0
    print(hook_output("SessionStart", "\n".join(lines)))
    mark_report_seen(fresh)
    return 0


def unseen_report():
    from brain.distill import REPORT
    cursor = REPORT.with_suffix(".seen")
    try:
        since = int(cursor.read_text())
    except (OSError, ValueError):
        since = 0
    out = []
    if REPORT.exists():
        for l in REPORT.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(l)
            except ValueError:
                continue
            if e.get("ts", 0) > since:
                out.append(e)
    return out


def mark_report_seen(entries):
    if not entries:
        return
    from brain.distill import REPORT
    try:
        REPORT.with_suffix(".seen").write_text(str(max(e.get("ts", 0) for e in entries)))
    except OSError:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
