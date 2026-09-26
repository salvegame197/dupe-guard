#!/usr/bin/env python3
"""
freshness — facts a note states that the disk can check.

A note lists modules, routes, files, tables. The disk answers those in one
command, and a stale list gives no error, shows up in no test, and is
exactly what a model reading the conversation does not catch (measured: a
note listed 3 crates with 10 on disk; three samples reading the whole
session missed it; `ls` answered at once). MemStrata's point applies:
contradiction of a checkable fact is settled by rule, not by similarity or
by a vote.

Version 1 checks PATHS: every `backtick/path.ext` mentioned in a project
note that no longer exists in the project's repository becomes a proposal
of kind `stale`; applying it marks the section as superseded, never deletes.
The repository of a note comes from the session queue (the last cwd that
mapped to it) or from `note_repos` in the config.

Usage:
    freshness.py             report
    freshness.py --record    also write the proposals
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, fold, is_dataless, load_config                # noqa: E402

QUEUE = DATA / "distill-queue.jsonl"
PATH_IN_TICKS = re.compile(r"`((?:[\w.@-]+/)+[\w.@-]+\.[A-Za-z0-9]{1,6})`")
HEADING = re.compile(r"^(#{2,3}) (.+)$", re.M)
# a line that talks about the path being gone is not a stale fact
GONE = re.compile(r"\b(removid|apagad|deletad|renomead|removed|deleted|renamed|dropped|antig|old |legacy|superado|superseded)", re.I)


def note_repos(cfg) -> dict:
    """note name -> repo path. The queue knows which cwd produced each note."""
    out = dict(cfg.get("note_repos") or {})
    if QUEUE.exists():
        for l in QUEUE.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                d = json.loads(l)
            except ValueError:
                continue
            if d.get("note") and d.get("cwd") and not d.get("orphan") and os.path.isdir(d["cwd"]):
                out[d["note"]] = d["cwd"]         # last one wins: most recent
    return out


def sections(text: str):
    """[(title, body)] for ## and ### sections; the preamble has title ''."""
    parts, last, title = [], 0, ""
    for m in HEADING.finditer(text):
        parts.append((title, text[last:m.start()]))
        title, last = m.group(2).strip(), m.end()
    parts.append((title, text[last:]))
    return parts


SUBDIRS = ("", "src", "app", "lib", "resources")


_TREES = {}


def tracked_anywhere(root: Path) -> set:
    """Every path in every local branch of the repo. The working tree is one
    checkout; a file the note cites may live on the feature branch."""
    key = str(root)
    if key not in _TREES:
        paths = set()
        try:
            refs = subprocess.run(["git", "-C", key, "for-each-ref", "--format=%(refname)", "refs/heads"],
                                  capture_output=True, text=True, timeout=10).stdout.split()
            for ref in refs[:40]:
                out = subprocess.run(["git", "-C", key, "ls-tree", "-r", "--name-only", ref],
                                     capture_output=True, text=True, timeout=30).stdout
                paths.update(out.split("\n"))
        except (subprocess.SubprocessError, OSError):
            pass
        _TREES[key] = paths
    return _TREES[key]


def exists_somewhere(p: str, roots) -> bool:
    """A note spans repositories: a delivery note cites files in the API, the
    admin and the front. A path counts as alive if ANY known repository has
    it, at its root or under the usual source folders; a path that starts
    with a repository's folder name is checked inside that repository."""
    head, _, rest = p.partition("/")
    for root in roots:
        root = Path(root)
        if rest and root.name == head and (root / rest).exists():
            return True
        for sub in SUBDIRS:
            if (root / sub / p).exists():
                return True
        tree = tracked_anywhere(root)
        if any((f"{sub}/{p}" if sub else p) in tree for sub in SUBDIRS) or (rest and root.name == head and rest in tree):
            return True
    return False


def stale_paths(note_text: str, roots):
    """[(section, path)] for paths mentioned that exist in no known repo."""
    out, seen = [], set()
    for title, body in sections(note_text):
        if "⚠️ **Super" in body[:400]:
            continue                        # already marked
        for line in body.splitlines():
            if GONE.search(line):
                continue
            for p in PATH_IN_TICKS.findall(line):
                if p.startswith(("/", "~", "http")) or (title, p) in seen:
                    continue
                seen.add((title, p))
                if not exists_somewhere(p, roots):
                    out.append((title, p))
    return out


def scan(cfg):
    """{note: {"repo": path, "stale": [(section, path)], "checked": n}}"""
    result = {}
    repos = note_repos(cfg)
    roots = sorted(set(repos.values()))
    for note, repo in repos.items():
        repo = Path(repo)
        found = [p for p in cfg["vault"].rglob(f"{note}.md") if ".obsidian" not in p.parts]
        if len(found) != 1 or is_dataless(found[0]):
            continue
        text = found[0].read_text(encoding="utf-8", errors="ignore")
        checked = len(set(PATH_IN_TICKS.findall(text)))
        if not checked:
            continue
        result[note] = {"repo": str(repo), "stale": stale_paths(text, roots), "checked": checked}
    return result


def main() -> int:
    cfg = load_config()
    res = scan(cfg)
    if not res:
        print("no project note with a known repository and paths to check.")
        return 0
    total = 0
    for note, r in res.items():
        print(f"{note}: {r['checked']} paths checked against {r['repo']}, {len(r['stale'])} missing")
        for section, p in r["stale"][:12]:
            print(f"   `{p}`  — section «{section or '(top)'}»")
        total += len(r["stale"])
    if "--record" in sys.argv and total:
        from brain import proposals
        props = [{"kind": "stale", "target": note,
                  "text": f"`{p}` no longer exists in {os.path.basename(r['repo'])} — section «{section or '(top)'}»"}
                 for note, r in res.items() for section, p in r["stale"]]
        n = proposals.record(props, [f"freshness: {sum(r['checked'] for r in res.values())} paths checked"],
                             f"fresh-{datetime.now().strftime('%Y%m%d')}", cfg)
        print(f"\nrecorded {n} proposal(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
