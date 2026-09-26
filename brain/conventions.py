#!/usr/bin/env python3
"""
conventions — rules that prove themselves, or retire.

A rule written once becomes law forever unless something counts against it.
ExpeL keeps its insights with a counter: new ones start at two, agreement
adds one, disagreement subtracts one, zero removes. Here:

  +1  the rule was injected in a session that edited the repo and ended
      with no admitted correction touching it: used without incident
  -1  the user corrected the model, the model admitted it, and the
      correction shares words with the rule: the rule misled
   0  the rule moves to the Superseded section with the date. Never
      deleted: a wrong label costs a line, a deletion costs the memory.

Creating a rule still goes through the user (a proposal); only the
bookkeeping is automatic. The counter lives in an HTML comment at the end
of the rule line, hidden by any markdown renderer.

Usage:
    conventions.py                      show every repo's rules and votes
    conventions.py --tally <transcript> apply one session's evidence
"""
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import HOME_DIR, RECALL_MARKS, fold, injected_context, labels, load_config   # noqa: E402

CONV_DIR = HOME_DIR / "conventions"
RULE = re.compile(r"^(?P<lead>\s*[-*] .+?)(?:\s*<!-- votes:(?P<v>-?\d+) -->)?\s*$")
INITIAL = 2
MIN_SHARED = 2
SUPERSEDED_HEADINGS = ("## Superseded", "## Superados")


def rule_lines(text: str):
    """[(line_index, rule_text, votes)] for rules in sections other than
    Superseded."""
    out, active = [], True
    for i, line in enumerate(text.splitlines()):
        if line.startswith("## "):
            active = not any(line.startswith(h) for h in SUPERSEDED_HEADINGS)
            continue
        if not active:
            continue
        m = RULE.match(line)
        if m and line.strip().startswith(("- ", "* ")):
            votes = int(m.group("v")) if m.group("v") is not None else INITIAL
            out.append((i, m.group("lead").strip(), votes))
    return out


def with_votes(lead: str, votes: int) -> str:
    return f"{lead} <!-- votes:{votes} -->"


def words(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{4,}", fold(s))}


def repo_file(repo: str) -> Path:
    return CONV_DIR / f"{repo}.md"


def tally_session(transcript: str, cfg) -> dict:
    """Evidence from one transcript: which repos had their rules injected,
    which repos were edited, and the admitted corrections (text)."""
    from brain.proposals import events, CORRECTION, ADMISSION, git_root_name
    ev = events(transcript)
    injected, edited, corrections = set(), set(), []
    # the guard injects "## Conventions for <repo>" once per session
    with open(transcript, "rb") as f:
        for raw in f:
            if b"Conventions for" not in raw and b"Convencoes de" not in raw:
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            a = d.get("attachment") or {}
            c = injected_context(a)
            for m in re.finditer(r"^## (?:Conventions for|Convencoes de) (.+?)(?: \(aprendidas\))?$", c, re.M):
                injected.add(m.group(1).strip())
    for ts, k, d in ev:
        if k == "tool" and d.get("name") in ("Edit", "Write", "MultiEdit") and d.get("path"):
            edited.add(git_root_name(str(Path(d["path"]).parent)))
    correction = re.compile("|".join(CORRECTION[l] for l in cfg["languages"]), re.I)
    admission = re.compile("|".join(ADMISSION[l] for l in cfg["languages"]), re.I)
    for i, (ts, k, c) in enumerate(ev):
        if k != "prompt" or len(c.strip()) < 15 or not correction.search(c):
            continue
        turn = ev[i + 1:i + 80]
        end = next((j for j, (_, k2, _) in enumerate(turn) if k2 == "prompt"), len(turn))
        reply = " ".join(t for _, k2, t in turn[:end] if k2 == "text")
        if admission.search(reply):
            corrections.append(c)
    return {"injected": injected, "edited": edited, "corrections": corrections}


def apply_votes(repo: str, edited: bool, corrections, cfg, today=None) -> dict:
    """Update one repo's counters; retire what reached zero. Returns counts."""
    f = repo_file(repo)
    if not f.exists():
        return {}
    L = labels(cfg)
    today = today or datetime.now().strftime("%Y-%m-%d")
    lines = f.read_text(encoding="utf-8").splitlines()
    up = down = retired = 0
    to_retire = []
    for i, lead, votes in rule_lines("\n".join(lines)):
        rw = words(lead)
        hit = any(len(rw & words(c)) >= MIN_SHARED for c in corrections)
        if hit:
            votes -= 1; down += 1
        elif edited:
            votes += 1; up += 1
        else:
            continue
        lines[i] = with_votes(lead, votes)
        if votes <= 0:
            to_retire.append(i)
    if to_retire:
        moved = []
        for i in sorted(to_retire, reverse=True):
            moved.append(lines.pop(i))
            retired += 1
        head = next((k for k, l in enumerate(lines) if any(l.startswith(h) for h in SUPERSEDED_HEADINGS)), None)
        block = [f"\n> ⚠️ **{L['superseded_on']} {today}**: {L['rule_retired']}"] + \
                [re.sub(r"\s*<!-- votes:-?\d+ -->", "", m) for m in moved]
        if head is None:
            lines += ["", SUPERSEDED_HEADINGS[0 if cfg["languages"][0] == "en" else 1]] + block
        else:
            lines[head + 1:head + 1] = block
    if up or down:
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"up": up, "down": down, "retired": retired}


def tally(transcript: str, cfg) -> dict:
    ev = tally_session(transcript, cfg)
    totals = {"up": 0, "down": 0, "retired": 0}
    for repo in ev["injected"]:
        r = apply_votes(repo, repo in ev["edited"], ev["corrections"], cfg)
        for k in totals:
            totals[k] += r.get(k, 0)
    return totals


def main() -> int:
    cfg = load_config()
    if sys.argv[1:2] == ["--tally"] and len(sys.argv) > 2:
        t = tally(sys.argv[2], cfg)
        print(f"votes: +{t['up']} -{t['down']}, retired {t['retired']}")
        return 0
    if not CONV_DIR.exists():
        print("no conventions yet.")
        return 0
    for f in sorted(CONV_DIR.glob("*.md")):
        if f.name.startswith("_"):
            continue
        rules = rule_lines(f.read_text(encoding="utf-8"))
        print(f"{f.stem}: {len(rules)} rule(s)")
        for _, lead, votes in rules:
            print(f"  [{votes:>2}] {lead[:90]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
