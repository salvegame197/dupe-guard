#!/usr/bin/env python3
"""
hit_rate — do the hooks help, or do they just spend context?

Reads the transcripts and, for each hook injection, looks at what the model
did in the tool calls that followed. It touches no hook: their output is
already recorded in the transcript, so measuring is entirely offline.

  guard   (PreToolUse / PostToolUse)  warned "already exists at X" -> did the
                                      model read X? edit the file again? cite
                                      the candidate?
  recall  (UserPromptSubmit)          injected vault pages -> was any read in
                                      the same turn? was ksearch called?

Usage:
  hit_rate.py                  every transcript
  hit_rate.py <session.jsonl>  one
  hit_rate.py --json
"""
import glob
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import RECALL_MARKS, injected_context, load_config       # noqa: E402

WINDOW = 6          # following tool calls that count as a "reaction" to the guard

GUARD_MARKS = ("Reuse: possible duplication", "Reuse: duplicated body",
               "Reuso: possivel duplicacao", "Reuso: corpo duplicado")
CAND = re.compile(r"`([^`:]+):(\d+)`")           # `path:line`
SYM = re.compile(r"\*\*`([^`]+)`\*\*")            # **`name`**
TITLE = re.compile(r"^\*\*(.+?)\*\* \(", re.M)    # **title** (origin


def events(path):
    """(ts, kind, data) for one transcript, only what matters here."""
    out, seen = [], set()
    with open(path, "rb") as f:
        for raw in f:
            if not any(k in raw for k in (b"hook_success", b"hook_additional_context",
                                          b'"tool_use"', b'"assistant"', b'"user"')):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            ts, t = d.get("timestamp") or "", d.get("type")
            if t == "attachment":
                a = d.get("attachment") or {}
                # Only the right hook on the right event. Other hooks echo their
                # payload on stdout, and the guard's text shows up in cat, in
                # manual tests, in heredocs; without this filter all of it
                # counts as a warning. One toolUseID is counted once.
                hn = a.get("hookName") or ""
                c = injected_context(a)
                tid = a.get("toolUseID")
                if not c or (tid and tid in seen):
                    continue
                if hn.startswith(("PreToolUse:", "PostToolUse:")) and any(m in c for m in GUARD_MARKS):
                    seen.add(tid)
                    out.append((ts, "guard", {"tool": tid, "text": c}))
                elif hn.startswith("UserPromptSubmit") and any(m in c for m in RECALL_MARKS):
                    seen.add(tid)
                    out.append((ts, "recall", {"text": c}))
            elif t == "assistant":
                for b in (d.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        out.append((ts, "tool", {"id": b.get("id"), "name": b.get("name"),
                                                 "path": inp.get("file_path") or "",
                                                 "cmd": inp.get("command") or ""}))
                    elif b.get("type") == "text":
                        out.append((ts, "text", {"text": b.get("text") or ""}))
            elif t == "user":
                c = (d.get("message") or {}).get("content")
                if isinstance(c, str) or (isinstance(c, list) and c
                                          and isinstance(c[0], dict) and c[0].get("type") == "text"):
                    out.append((ts, "prompt", {}))
    return out


def analyse(path, acc):
    ev = events(path)
    for i, (ts, kind, d) in enumerate(ev):
        if kind == "guard":
            c = d["text"]
            cands = {p for p, _ in CAND.findall(c)}
            syms = set(SYM.findall(c))
            # the guarded Write/Edit: the tool_use with that id, for its file
            target = next((e[2]["path"] for e in ev if e[1] == "tool" and e[2]["id"] == d["tool"]), "")
            read = revised = cited = False
            n = 0
            for _, k2, d2 in ev[i + 1:]:
                if k2 == "prompt":
                    break
                if k2 == "text":
                    cited = cited or any(s in d2["text"] for s in syms)
                    continue
                if k2 != "tool":
                    continue
                n += 1
                if n > WINDOW:
                    break
                p = d2["path"]
                if d2["name"] == "Read" and any(p.endswith(cd) for cd in cands):
                    read = True
                if d2["name"] in ("Edit", "Write", "MultiEdit") and target and p == target:
                    revised = True
            g = acc["guard"]
            g["warnings"] += 1
            g["read"] += read
            g["revised"] += revised
            g["cited"] += cited
            g["ignored"] += not (read or revised or cited)
            g["chars"] += len(c)
        elif kind == "recall":
            c = d["text"]
            titles = {t.strip().lower() for t in TITLE.findall(c)}
            used = deeper = False
            for _, k2, d2 in ev[i + 1:]:
                if k2 == "prompt":
                    break
                if k2 != "tool":
                    continue
                if d2["name"] == "Read":
                    used = used or os.path.splitext(os.path.basename(d2["path"]))[0].lower() in titles
                if d2["name"] == "Bash" and "ksearch" in d2["cmd"]:
                    deeper = True
            r = acc["recall"]
            r["injections"] += 1
            r["pages"] += len(titles)
            r["used"] += used
            r["deeper"] += deeper
            r["chars"] += len(c)


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "   —  "


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    files = args or sorted(glob.glob(str(load_config()["transcripts"] / "*" / "*.jsonl")))
    acc = {"guard": Counter(), "recall": Counter()}
    for f in files:
        try:
            analyse(f, acc)
        except (OSError, ValueError):
            continue
    if "--json" in sys.argv:
        print(json.dumps({"transcripts": len(files), **acc}))
        return 0
    g, r = acc["guard"], acc["recall"]
    print(f"transcripts analysed: {len(files)}\n")
    print("GUARD")
    print(f"  warnings               {g['warnings']:>6}   ~{g['chars'] // 4:,} tokens injected")
    print(f"  read the candidate     {g['read']:>6}   {pct(g['read'], g['warnings'])}")
    print(f"  edited the file again  {g['revised']:>6}   {pct(g['revised'], g['warnings'])}")
    print(f"  cited the candidate    {g['cited']:>6}   {pct(g['cited'], g['warnings'])}")
    print(f"  ignored (none of it)   {g['ignored']:>6}   {pct(g['ignored'], g['warnings'])}")
    print()
    print("RECALL")
    print(f"  injections             {r['injections']:>6}   ~{r['chars'] // 4:,} tokens injected")
    print(f"  pages suggested        {r['pages']:>6}")
    print(f"  read one that turn     {r['used']:>6}   {pct(r['used'], r['injections'])}")
    print(f"  searched deeper        {r['deeper']:>6}   {pct(r['deeper'], r['injections'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
