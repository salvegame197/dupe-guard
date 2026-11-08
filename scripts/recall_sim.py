#!/usr/bin/env python3
"""
recall_sim — what the 2026-11-08 recall rules would have done to the past.

Read-only. Two sources, two questions:

  recall-log.jsonl   of every prompt the recall evaluated, how many would now
                     be skipped as not human (brain/recall.py:non_human), and
                     of every page injected, how many came from each vault
                     folder (title -> folder through the search index).
  transcripts        for every recall injection recorded in a transcript, was
                     the session a code session (its cwd inside a git repo),
                     and how many of the injected pages the code-session mute
                     (`code_session_mute`, default Claude Web/) would have
                     dropped.

The log keeps 80 characters of the prompt, so the "short" rule is only
approximate there; every prefix rule is exact.

Usage:
    python3 scripts/recall_sim.py            # both
    python3 scripts/recall_sim.py --log      # log only (fast)
"""
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain import hit_rate, recall                                           # noqa: E402
from brain.core import WORKER_DIR, load_config, project_folder_name          # noqa: E402

TITLE_ORIGIN = re.compile(r"^\*\*(.+?)\*\* \(([^,)]+)", re.M)


def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "   —  "


def simulate_log(log_path: Path):
    by_title = hit_rate.title_folders()
    total = 0
    skipped = Counter()
    skipped_were = Counter()          # what the old code decided on those prompts
    folders = Counter()
    items = 0
    with log_path.open(encoding="utf-8") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            total += 1
            why = recall.non_human(d.get("prompt") or "")
            if why:
                skipped[why] += 1
                skipped_were[d.get("decision")] += 1
            ids = d.get("ids") or []
            for i, (title, _s) in enumerate(d.get("picked") or []):
                items += 1
                folders[hit_rate.folder_of(title, ids[i] if i < len(ids) else "", by_title)] += 1
    n_skip = sum(skipped.values())
    print(f"recall-log: {total} decisions")
    print(f"  would be skip:non-human  {n_skip:>6}   {pct(n_skip, total)}")
    for why, n in skipped.most_common():
        print(f"    {why:<22} {n:>6}")
    print("  of those, the old code decided: " + ", ".join(f"{k} {v}" for k, v in skipped_were.most_common()))
    print(f"  pages injected           {items:>6}   by vault folder:")
    for folder, n in folders.most_common(8):
        print(f"    {folder:<22} {n:>6}   {pct(n, items)}")
    return folders, items


def cwd_of(path):
    with open(path, "rb") as f:
        for raw in f:
            if b'"cwd"' not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if d.get("cwd"):
                return d["cwd"]
    return ""


def simulate_transcripts(cfg):
    muted = tuple(cfg.get("code_session_mute") or ())
    by_title = hit_rate.title_folders()
    files = sorted(f for f in glob.glob(str(cfg["transcripts"] / "*" / "*.jsonl"))
                   if Path(f).parent.name != project_folder_name(WORKER_DIR))
    sessions = Counter()
    injections = Counter()
    pages = Counter()
    muted_pages = Counter()
    for f in files:
        try:
            ev = hit_rate.events(f)
        except (OSError, ValueError):
            continue
        rec = [d for _, k, d in ev if k == "recall"]
        if not rec:
            continue
        code = recall.in_git_repo(cwd_of(f))
        mode = "code" if code else "chat"
        sessions[mode] += 1
        for d in rec:
            injections[mode] += 1
            titles = TITLE_ORIGIN.findall(d["text"])
            for title, origin in titles:
                folder = hit_rate.folder_of(title.strip(), "", by_title) if origin == "vault" else "transcript"
                pages[(mode, folder)] += 1
                if code and folder != "?" and folder.startswith(muted):
                    muted_pages[folder] += 1
    tot_inj = sum(injections.values())
    tot_pages = sum(pages.values())
    print(f"\ntranscripts: {sum(sessions.values())} sessions with injections "
          f"(code {sessions['code']}, chat {sessions['chat']}); {tot_inj} injections "
          f"(code {injections['code']}, chat {injections['chat']})")
    print(f"  pages injected           {tot_pages:>6}")
    for mode in ("code", "chat"):
        sub = {k[1]: v for k, v in pages.items() if k[0] == mode}
        n = sum(sub.values())
        print(f"  in {mode} sessions        {n:>6}   by vault folder:")
        for folder, c in sorted(sub.items(), key=lambda x: -x[1])[:6]:
            print(f"    {folder:<22} {c:>6}   {pct(c, n)}")
    n_m = sum(muted_pages.values())
    print(f"  dropped by the code-session mute {muted or '(none)'}: {n_m} of {tot_pages} pages "
          f"({pct(n_m, tot_pages).strip()}); of the code-session pages: "
          f"{pct(n_m, sum(v for k, v in pages.items() if k[0] == 'code')).strip()}")


def main():
    cfg = load_config()
    simulate_log(recall.LOG)
    if "--log" not in sys.argv:
        simulate_transcripts(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
