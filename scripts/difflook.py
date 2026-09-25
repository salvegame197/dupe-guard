#!/usr/bin/env python3
"""
difflook — what the diff INTRODUCED, and what already existed that looks like it.

Does not judge: gathers evidence. For each new symbol in the diff it lists the
reuse candidates already in the repo. The judgement ("this should have reused
X") belongs to the model, with the code in front of it.

Usage:
    difflook.py                 working diff (staged + unstaged)
    difflook.py --base main     everything the branch changed against main
    difflook.py --json
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import csearch                                              # noqa: E402

MIN_SCORE = 7.0
MAX_HITS = 3


def git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                       text=True, timeout=60)
    return r.stdout if r.returncode == 0 else ""


HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)")


def added_lines(diff: str):
    """{file: [(line, text)]} — only what came in."""
    files, cur, ln = {}, None, 0
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            cur = line[6:]
            files.setdefault(cur, [])
            continue
        m = HUNK.match(line)
        if m:
            ln = int(m.group(1))
            continue
        if cur is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            files[cur].append((ln, line[1:]))
            ln += 1
        elif not line.startswith("-"):
            ln += 1
    return {f: v for f, v in files.items() if v}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", help="compare against this ref (e.g. main)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo")
    a = ap.parse_args()

    root = csearch.repo_root(Path(a.repo).resolve() if a.repo else Path.cwd())

    if a.base:
        diff = git(["diff", f"{a.base}...HEAD"], root)
        before_ref = git(["merge-base", a.base, "HEAD"], root).strip() or a.base
    else:
        diff = git(["diff", "HEAD"], root) or git(["diff"], root)
        before_ref = "HEAD"

    if not diff.strip():
        print("no changes in the diff.")
        return 0

    data = csearch.load(root)
    report = []

    for rel, lines in added_lines(diff).items():
        ext = os.path.splitext(rel)[1]
        lang = csearch.EXTS.get(ext)
        if not lang or csearch.SKIP_FILE.search(os.path.basename(rel)):
            continue

        text = "\n".join(t for _, t in lines)
        syms = csearch.extract_text(text, lang)
        if not syms:
            continue

        # A declaration already in the file before the diff is not news: the
        # symbol only shows up here because its line was rewritten. "Before"
        # is the merge base when comparing a branch; HEAD would be the NEW
        # version and hide every new symbol.
        before = set()
        old = git(["show", f"{before_ref}:{rel}"], root)
        if old:
            before = {y["n"] for y in csearch.extract_text(old, lang)}

        entry = {"file": rel, "new": []}
        for sym in syms:
            if sym["n"] in before:
                continue
            hits = [h for h in csearch.search(data, sym["n"], MAX_HITS, rel)
                    if h["score"] >= MIN_SCORE]
            entry["new"].append({
                "name": sym["n"], "kind": sym["k"], "candidates": hits,
            })
        if entry["new"]:
            report.append(entry)

    if a.json:
        print(json.dumps({"root": str(root), "files": report}, indent=2))
        return 0

    if not report:
        print("the diff introduces no new symbol (it only changed existing ones).")
        return 0

    for e in report:
        print(f"\n## {e['file']}")
        for s in e["new"]:
            mark = "!" if s["candidates"] else " "
            print(f" {mark} {s['kind']:<5} {s['name']}")
            for c in s["candidates"]:
                print(f"       already exists: {c['path']}:{c['line']}")
                print(f"                       {c['sig'][:100]}")
    print("\n('!' = there is a reuse candidate; read it before approving the new code)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
