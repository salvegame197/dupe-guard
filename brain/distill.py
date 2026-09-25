#!/usr/bin/env python3
"""
distill — turn a session's raw trace into understanding in the project note.

The EXPENSIVE level of the pair that starts in session_log.py. They are split
because one hook doing both ends up either too expensive to always run or too
dumb to be worth it:

  - session_log.py (SessionEnd, always, ~0.1s, no LLM) records what is
    verifiable: repo, branch, requests, files, commits. And queues.
  - distill.py (this, on demand) reads the queue and calls `claude -p` to
    write what only a model can: decision, root cause, pitfall.

It uses the user's own Claude Code (`claude -p`): no API key to configure, and
it counts against their plan like any other session.

The default is DRY-RUN: show what would be written and touch nothing. Until
the quality of the distillate is proven on a vault, writing straight into the
notes bets the one durable store on an untested prompt.

Usage:
    distill.py                 # show what it would do (writes nothing)
    distill.py --apply         # write into the notes and clear the queue
    distill.py --limit 3       # only the N oldest pending sessions
    distill.py --stale <note>  # only the staleness check on a note
"""

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, load_config                                # noqa: E402

QUEUE = DATA / "distill-queue.jsonl"
DONE = DATA / "distill-done.jsonl"
TIMEOUT = 300
CLAUDE = os.environ.get("DUPE_GUARD_CLAUDE", "claude")

INSTRUCTIONS = """You are distilling ONE work session into the project note of \
a second brain kept as markdown.

Write ONLY the markdown block to be APPENDED to the note. No preamble, no \
"here it is". If the session produced no new understanding — only reading, \
investigation without a conclusion, or what the note already says — answer \
exactly EMPTY and nothing else.

Format (follow the style already in the note):
- The FIRST LINE of your answer must be a heading (`##` or `###`) with a short
  title. Never start with a table row (`| ... |`), a bullet or loose text, even
  if the note's history is a table. A table is an index format; you are writing
  a section.
- Do NOT put any date in the title: the program stamps it afterwards, from the
  session record.
- Heading LEVEL: the same as the note's dated sibling sections. If its history
  is under `## Wave 7 (Aug 2026)`, write `##`; if it is `### Subject (date)`
  inside a larger section, write `###`. Look at the note and follow it.
- Write in {note_language}; concise.
- Record DECISIONS, ROOT CAUSES and PITFALLS. Do not narrate what was done, do
  not paste code, do not list files (the raw log already has them).
- Mark a deliberate decision by the user with 🔑 and a pitfall/gotcha with ⚠️.
- Use [[wikilinks]] for projects and concepts already cited in the note, but
  never for the note being edited.
- If something was decided AGAINST an alternative, say which and why: that is
  what stops someone (an agent included) from "fixing" it back later.

Most important rule: DO NOT DUPLICATE what the note already says. If the point
is there, leave it out. Three true lines beat twenty repeated ones.

Hard case — CONTRADICTION. If the session reverted, corrected or made obsolete
something the note states, do not write the new next to the old as if both
held. Open the block saying what fell, citing the old section's title, and
why it changed. Anyone notices duplication by reading; nobody notices a silent
contradiction, and the note starts lying with the authority of being written.
If the session PARTLY covers something already there, write only the delta and
say which section it adds to."""

EMPTY = ("EMPTY", "VAZIO")

# Closing and forward-looking sections, in English and Portuguese. A new block
# goes right BEFORE the first of them: history comes before anything that
# talks about what has not been done yet.
ANCHOR = re.compile(
    r"^## (Sessions|Related|Links|References|See also|Future|Next|Roadmap|Backlog|"
    r"Pending|Ideas|TODO|"
    r"Sessoes|Sessões|Relacionados|Hubs Relacionados|Referencias|Referências|Futuro|Norte|"
    r"Proximos|Próximos|Pendencias|Pendências|Ideias)\b",
    re.M,
)


def is_empty(out: str) -> bool:
    return not out or out.strip().upper().startswith(EMPTY)


def queue(cfg, limit=None, note=None):
    """Pending entries, deduplicated by session (the last one wins)."""
    if not QUEUE.exists():
        return []
    done = set()
    if DONE.exists():
        for l in DONE.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                done.add(json.loads(l)["session_id"])
            except (ValueError, KeyError):
                pass
    items = OrderedDict()
    for l in QUEUE.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            d = json.loads(l)
        except ValueError:
            continue
        if d.get("session_id") in done or not d.get("note") or d.get("orphan"):
            continue        # no note, or an orphan: nowhere to write yet
        if note and d["note"] != note:
            continue
        items[d["session_id"]] = d
    # CHRONOLOGICAL order, not the order the queue was written. It matters
    # because the note is built by appending: running an older session after a
    # newer one makes the old decision "win", a silent contradiction.
    out = sorted(items.values(), key=lambda d: d.get("ts") or "")
    return out[:limit] if limit else out


def note_path(vault: Path, name: str):
    """
    The real note, or None with the reason — AMBIGUITY ABORTS.

    If the vault has more than one file with that name, pick neither the first
    nor the largest: stop and report the conflict. Writing into the wrong note
    is the damage that raises no error, the one case where guessing is not
    worth it.
    """
    every = [p for p in vault.rglob(f"{name}.md") if ".obsidian" not in p.parts]
    alive = [p for p in every if p.stat().st_size > 0]
    if len(alive) > 1:
        where = ", ".join(str(p.relative_to(vault)) for p in alive)
        return None, f"ambiguous name: {len(alive)} notes '{name}.md' ({where})"
    if not alive:
        return None, (f"note '{name}.md' is empty" if every else f"note '{name}.md' does not exist")
    return alive[0], None


def lint(vault: Path, items):
    """Checks on the vault before writing. Empty duplicates and missing anchors
    are exactly what sends a block to the wrong place."""
    problems, seen, names = [], set(), {}
    for p in vault.rglob("*.md"):
        if ".obsidian" in p.parts:
            continue
        names.setdefault(p.stem, []).append(p)
        if p.stat().st_size == 0:
            problems.append(("empty", f"{p.relative_to(vault)} has 0 bytes"))
    for name, ps in names.items():
        if len(ps) > 1:
            where = ", ".join(str(x.relative_to(vault)) for x in ps)
            problems.append(("duplicate", f"'{name}.md' appears {len(ps)}x: {where}"))
    for it in items:
        if it["note"] in seen:
            continue
        seen.add(it["note"])
        note, err = note_path(vault, it["note"])
        if err:
            problems.append(("target", f"{it['note']}: {err}"))
        elif not ANCHOR.search(note.read_text(encoding="utf-8", errors="ignore")):
            problems.append(("anchor", f"{it['note']}: no closing section — "
                                       f"the block would land at the end of the file"))
    return problems


def raw_block(item) -> str:
    """The block the hook already wrote: cheap, reliable input."""
    try:
        txt = Path(item["raw"]).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    mark = f"<!-- sid:{item['session_id']}"
    i = txt.find(mark)
    if i < 0:
        return ""
    end = txt.find("<!-- sid:", i + 10)
    return txt[i:end if end > 0 else len(txt)]


MAX_TURN = 1500
BUDGET = 60000


def digest(transcript: str) -> str:
    """
    The readable conversation, extracted from the transcript with no LLM.

    The raw log (requests cut at 200 chars, a list of files) does not carry
    what makes a decision a decision: the why, the discarded alternative, what
    broke halfway. With only that, the distiller answers EMPTY for lack of
    material.

    Giving `claude -p` tools to read the transcript itself does NOT work: in
    -p mode stdin was consumed by the prompt, so any permission request waits
    for an answer that never comes and the process hangs until the timeout.

    Cut by priority when over budget: every user turn stays (that is where
    decisions are made) and replies are added from the end backwards, because
    conclusions live at the end.
    """
    user, model = [], []
    try:
        fh = open(transcript, encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            t = d.get("type")
            if t == "user" and not d.get("isMeta"):
                c = (d.get("message") or {}).get("content")
                if isinstance(c, str):
                    s = c.strip()
                    if s and not s.startswith("<") and not s.startswith("/"):
                        user.append(("user", s[:MAX_TURN], len(user) + len(model)))
            elif t == "assistant":
                for b in (d.get("message") or {}).get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "text":
                        s = (b.get("text") or "").strip()
                        if len(s) > 80:      # "ok", "done" explain nothing
                            model.append(("claude", s[:MAX_TURN], len(user) + len(model)))
    chosen = list(user)
    cost = sum(len(x[1]) for x in chosen)
    for turn in reversed(model):
        if cost + len(turn[1]) > BUDGET:
            break
        chosen.append(turn)
        cost += len(turn[1])
    chosen.sort(key=lambda x: x[2])
    return "\n\n".join(f"[{who}] {txt}" for who, txt, _ in chosen)


def validate(block: str):
    """
    Refuse malformed output before it enters the note. A session once came out
    as a loose TABLE ROW — the model imitated the note's session table, obeying
    "follow the style" — and broken markdown in the vault raises no error, it
    just disappears into the text.
    """
    line = (block.lstrip().splitlines() or [""])[0].strip()
    if not re.match(r"^#{2,3} \S", line):
        return f"the output does not start with a heading (it starts with: {line[:60]!r})"
    return None


def stamp_date(block: str, day: str) -> str:
    """
    Put the session's date in the title — in code, not in the prompt.

    Asking the model for the date failed even with an explicit instruction:
    `claude -p` carries today's date in its own system prompt and it wins.
    Dating the past with today silently poisons the note. Data the program
    KNOWS, the program writes.
    """
    lines = block.splitlines()
    for i, l in enumerate(lines):
        m = re.match(r"^(#{2,3}) (.+?)\s*$", l)
        if not m:
            continue
        # Remove a date ANYWHERE, not just at the end: the model writes
        # "Title (2026-09-10) 🔑" and an end-anchored pattern misses it.
        title = re.sub(r"\s*\((?:\d{4}-\d{2}-\d{2}|\d{1,2}/\w+/\d{4}|[^)]{0,20}\d{4})\)",
                       "", m.group(2))
        title = re.sub(r"\s{2,}", " ", title).strip()
        lines[i] = f"{m.group(1)} {title} ({day})"
        break
    return "\n".join(lines)


def ask(prompt: str, model: str):
    """(ok, stdout or error)."""
    try:
        r = subprocess.run([CLAUDE, "-p", "--model", model], input=prompt,
                           capture_output=True, text=True, timeout=TIMEOUT)
    except (subprocess.SubprocessError, OSError) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, (r.stderr or "").strip()[:200]
    return True, (r.stdout or "").strip()


def distill(item, note: Path, cfg) -> str:
    conversation = digest(item["transcript"])
    if not conversation:
        return ""
    current = note.read_text(encoding="utf-8", errors="ignore")
    prompt = (
        f"{INSTRUCTIONS.replace('{note_language}', cfg['note_language'])}\n\n"
        f"=== CURRENT NOTE ({note.name}) ===\n{current}\n\n"
        f"=== SESSION FACTS (from the hook, verified) ===\n"
        f"Session date: {item['day']}\n{raw_block(item)}\n\n"
        f"=== CONVERSATION ===\n{conversation}\n\n"
        f"Now write the block, or EMPTY."
    )
    ok, out = ask(prompt, cfg["distill_model"])
    if not ok:
        return f"__ERROR__ {out}"
    if is_empty(out):
        return out
    if validate(out):
        # One retry with the rule on top: cheap, and the failure is one of
        # format, not judgement — the content is usually right.
        ok, out = ask(prompt + "\n\nATTENTION: your answer MUST start with a `## ` or "
                               "`### ` line. Do not start with a table or a bullet.",
                      cfg["distill_model"])
        if not ok:
            return f"__ERROR__ {out}"
        if is_empty(out):
            return out
        err = validate(out)
        if err:
            return f"__ERROR__ invalid format after 2 attempts: {err}"
    return stamp_date(out, item["day"])


def insert(note: Path, block: str, sid: str) -> bool:
    """
    Insert right BEFORE the first closing section (Sessions, Related, Links,
    Future...), i.e. at the end of the substantive content. Appending at the
    end of the file would put new history after closing and forward-looking
    sections. The session marker makes it idempotent.
    """
    txt = note.read_text(encoding="utf-8", errors="ignore")
    if f"<!-- distilled:{sid} -->" in txt:
        return False
    body = block.rstrip() + f"\n\n<!-- distilled:{sid} -->\n"
    anchor = ANCHOR.search(txt)
    if anchor:
        new = txt[:anchor.start()].rstrip() + "\n\n" + body + "\n" + txt[anchor.start():]
    else:
        new = txt.rstrip() + "\n\n" + body
    note.write_text(new, encoding="utf-8")
    return True


# ─── staleness ───────────────────────────────────────────────────────────

CHECKLIST = """Below are a second-brain NOTE and a NEW BLOCK that was just \
appended to it.

For EACH numbered section of the note, answer one question:
**does the new block make ANY statement in that section FALSE?**

CONTRADICTS — the section states something that is now wrong: a number that
              changed, a list that is no longer that list, a decision that was
              reverted, a mechanism that was replaced.
NO          — everything else.

These are NOT contradictions, so answer NO:
  - the section being incomplete, or "missing" something the block adds;
  - the section "could mention" or "would benefit from" something in the block;
  - the block detailing better a subject the section covers briefly;
  - the section being old.
Incompleteness is solved by addition, and the new block already did that.
Only a contradiction requires marking what was there.

Answer ONE LINE PER SECTION, every section, in order, exactly like this:
<number> | CONTRADICTS|NO | which statement became false, in one line (empty if NO)

Nothing before, nothing after. Go through the whole list even if nearly
everything is NO."""

# Sections that are an INDEX, not knowledge. Two detections, because neither
# holds alone: a name list will not cover the next "## See also", and the
# structural test alone would misfire on a short section full of links.
INDEX_SECTION = re.compile(
    r"^#{2,3} (Sessions|Related|Links|References|Index|Tags|See also|"
    r"Sessoes|Sessões|Relacionados|Hubs Relacionados|Referencias|Referências|Indice|Índice|"
    r"Ver tambem|Ver também)\b",
    re.M,
)


def is_pointer(body: str) -> bool:
    """A section whose body is mostly wikilinks, table rows or link lists. A
    pointer has nothing to supersede: what ages is the link's target."""
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    if not lines:
        return True
    pointing = sum(1 for l in lines
                   if l.startswith("|") or l.startswith("![[")
                   or (("[[" in l or "](" in l)
                       and len(re.sub(r"\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)", "", l)) < 40))
    return pointing / len(lines) > 0.6


def sections(txt: str, skip_marker: str = ""):
    """## and ### headings, minus the freshly inserted block and minus indexes."""
    new_end = txt.find(f"<!-- distilled:{skip_marker} -->") if skip_marker else -1
    new_start = txt.rfind("\n## ", 0, new_end) if new_end > 0 else -1
    if new_start < 0:
        new_start = txt.rfind("\n### ", 0, new_end) if new_end > 0 else -1
    out = []
    for m in re.finditer(r"^(#{2,3}) (.+)$", txt, re.M):
        if new_start >= 0 <= new_end and new_start <= m.start() <= new_end:
            continue
        if INDEX_SECTION.match(m.group(0)):
            continue
        nxt = re.search(r"^#{2,3} ", txt[m.end():], re.M)
        body = txt[m.end():m.end() + (nxt.start() if nxt else 2000)]
        if is_pointer(body):
            continue
        out.append((m.group(1), m.group(2).strip(), m.start()))
    return out


def queue_item(sid: str):
    """A queue entry by session, processed or not (the queue is append-only)."""
    if not QUEUE.exists():
        return None
    for l in QUEUE.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            d = json.loads(l)
        except ValueError:
            continue
        if d.get("session_id") == sid:
            return d
    return None


def staleness(note: Path, sid: str, model: str, samples: int):
    """
    What the new block left out of date in the rest of the note.

      1. A CHECKLIST, not an open question. Enumerating the sections and
         demanding a verdict for each removes the "remembered to look at that
         section" intermittence.
      2. SEVERAL SAMPLES. The same prompt on the same session flagged a section
         in one run and missed it in another; votes across samples decide.
    """
    txt = note.read_text(encoding="utf-8", errors="ignore")
    listed = sections(txt, sid)
    if not listed:
        return []
    enum = "\n".join(f"{i + 1}. {lvl} {tit}" for i, (lvl, tit, _) in enumerate(listed))
    mark = f"<!-- distilled:{sid} -->"
    cut = txt.find(mark)
    start = max(txt.rfind("\n## ", 0, cut), txt.rfind("\n### ", 0, cut))
    block = txt[start:cut] if cut > 0 and start > 0 else ""

    # The CONVERSATION goes in too, not just the block: what the summary left
    # out, the checker cannot recover. The source of truth about the session
    # is the session.
    it = queue_item(sid)
    conversation = digest(it["transcript"]) if it else ""
    prompt = (f"{CHECKLIST}\n\n=== NOTE SECTIONS ===\n{enum}\n\n"
              f"=== FULL NOTE ===\n{txt}\n\n"
              f"=== NEW BLOCK (summary already written) ===\n{block}\n\n"
              + (f"=== SESSION CONVERSATION (full source; the block may have "
                 f"left things out) ===\n{conversation}\n\n" if conversation else "")
              + f"Now the {len(listed)} lines.")

    with ThreadPoolExecutor(max_workers=samples) as ex:
        outs = list(ex.map(lambda _: ask(prompt, model), range(samples)))

    found = {}
    for ok, out in outs:
        for line in (out if ok else "").splitlines():
            m = re.match(r"\s*(\d+)\s*\|\s*(CONTRADICTS|NO)\s*\|?\s*(.*)", line.strip())
            if not m or m.group(2) != "CONTRADICTS":
                continue
            idx, reason = int(m.group(1)) - 1, m.group(3).strip()
            if not 0 <= idx < len(listed):
                continue
            lvl, tit, _ = listed[idx]
            f = found.setdefault(tit, {"section": f"{lvl} {tit}", "reason": reason, "votes": 0})
            f["votes"] += 1
            if len(reason) > len(f["reason"]):
                f["reason"] = reason
    return sorted(found.values(), key=lambda i: -i["votes"])


def mark_superseded(note: Path, found, origin: str, samples: int) -> int:
    """
    Mark a superseded section right under its heading. Never delete.

    A review queue would depend on someone sitting down to review, and that is
    how sessions get lost. If the mark is wrong, the damage is a wrongly
    labelled section; if it is right, the note stopped lying. With git
    underneath, both are reversible, so marking beats asking. A MAJORITY of
    samples is required: the mark lands in the note with nobody in between.
    """
    if not found:
        return 0
    txt = note.read_text(encoding="utf-8", errors="ignore")
    marked = 0
    for it in found:
        if it["votes"] * 2 <= samples:
            continue
        m = re.search(rf"^{re.escape(it['section'])}\s*$", txt, re.M)
        if not m:
            continue
        if re.search(r"⚠️ \*\*(Superseded|Superado)", txt[m.end():m.end() + 400]):
            continue          # already marked (idempotency)
        line = (f"\n\n> ⚠️ **Superseded by {origin}** "
                f"— {it['reason'] or 'see the most recent block'} "
                f"_(marked automatically, {it['votes']}/{samples})_")
        txt = txt[:m.end()] + line + txt[m.end():]
        marked += 1
    if marked:
        note.write_text(txt, encoding="utf-8")
    return marked


def preview(note: Path, block: str, sid: str) -> str:
    """Unified diff of what the insertion would do, without touching the file."""
    before = note.read_text(encoding="utf-8", errors="ignore")
    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / note.name
        copy.write_text(before, encoding="utf-8")
        insert(copy, block, sid)
        after = copy.read_text(encoding="utf-8")
    return "".join(difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                        fromfile=f"a/{note.name}", tofile=f"b/{note.name}", n=3))


def run_stale(cfg, name: str, samples: int, apply: bool) -> int:
    """A standalone check, for a note that got a block in an earlier run."""
    note, err = note_path(cfg["vault"], name)
    if err:
        print(err)
        return 1
    txt = note.read_text(encoding="utf-8", errors="ignore")
    sids = re.findall(r"<!-- distilled:([^ ]+) -->", txt)
    if not sids:
        print(f"{name}: no distilled block in this note.")
        return 1
    sid = sids[-1]
    found = staleness(note, sid, cfg["distill_model"], samples)
    title = "the most recent block"
    head = re.findall(r"^#{2,3} (.+)$", txt[:txt.find(f"<!-- distilled:{sid}")], re.M)
    if head:
        title = head[-1]
    print(f"{name} › {len(found)} item(s) over {samples} samples\n")
    for it in found:
        print(f"  [{it['votes']}/{samples}] CONTRADICTS  {it['section']}")
        print(f"      {it['reason']}")
    if apply:
        print(f"\n✓ {mark_superseded(note, found, title, samples)} section(s) marked in the note")
    else:
        print(f"\nTo mark them in the note: --stale {name} --apply")
    return 0


def commit(vault: Path, note: Path, item):
    """One commit per note, not per batch: reverting one bad distillation must
    not throw away the others."""
    msg = (f"{item['note']}: distilled from the session of {item['day']}\n\n"
           f"Written by distill.py (session {item['session_id'][:8]}).\n"
           f"To revert just this block: git revert <this commit>.")
    try:
        subprocess.run(["git", "-C", str(vault), "add", str(note)], capture_output=True, timeout=30)
        subprocess.run(["git", "-C", str(vault), "commit", "-m", msg], capture_output=True, timeout=30)
    except (subprocess.SubprocessError, OSError):
        pass


def mark_done(item):
    try:
        DONE.parent.mkdir(parents=True, exist_ok=True)
        with DONE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"session_id": item["session_id"], "note": item["note"],
                                 "at": datetime.now().isoformat()}) + "\n")
    except OSError:
        pass


def main() -> int:
    cfg = load_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="really write")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--model", default=cfg["distill_model"])
    ap.add_argument("--note", help="only the sessions of one note")
    ap.add_argument("--stale", metavar="NOTE", help="only the staleness check on a written note")
    ap.add_argument("--samples", type=int, default=cfg["distill_samples"])
    ap.add_argument("--commit", action="store_true", help="one commit per note written")
    ap.add_argument("--no-gate", action="store_true", help="do not stop after the first note")
    args = ap.parse_args()
    cfg["distill_model"] = args.model
    vault = cfg["vault"]

    if args.stale:
        return run_stale(cfg, args.stale, args.samples, args.apply)

    items = queue(cfg, args.limit, args.note)
    if not items:
        print("queue empty — nothing to distill.")
        return 0

    # The lint is a gate, not a warning: it only counts if it prevents writing.
    if args.apply:
        problems = lint(vault, items)
        if problems:
            print("vault lint failed — nothing was written:\n")
            for kind, msg in problems:
                print(f"  [{kind}] {msg}")
            return 1

    written = 0
    print(f"{len(items)} session(s) in the queue"
          f"{'' if args.apply else '  [DRY-RUN: nothing will be written]'}\n")
    for it in items:
        note, err = note_path(vault, it["note"])
        if err:
            print(f"— {it['note']}: {err} — skipping")
            continue
        print(f"=== {it['note']}  ({it['day']}, sid {it['session_id'][:8]}) ===")
        out = distill(it, note, cfg)

        if out.startswith("__ERROR__"):
            print(f"  failed: {out[10:]}\n")
            continue
        if not args.apply and not is_empty(out):
            # A diff, not the loose block: the text can be right and land in
            # the wrong place.
            print(preview(note, out, it["session_id"]))
        if is_empty(out):
            print("  nothing new to record.\n")
            if args.apply:
                mark_done(it)
            continue
        if args.apply:
            if insert(note, out, it["session_id"]):
                print(f"  ✓ written to {note.relative_to(vault)}")
                title = re.search(r"^#{2,3} (.+)$", out, re.M)
                n = mark_superseded(note, staleness(note, it["session_id"], args.model, args.samples),
                                    title.group(1) if title else it["day"], args.samples)
                print(f"  ✓ {n} section(s) marked as superseded\n" if n
                      else "  nothing became obsolete\n")
            if args.commit:
                commit(vault, note, it)
            mark_done(it)
            written += 1
            # GATE: stop after the FIRST note and hand control back. Reading
            # the first of every batch is a mechanism, not a discipline; the
            # defects that lint and dry-run miss show up there.
            if written == 1 and not args.no_gate and len(items) > 1:
                print(f"\n── GATE ──\nFirst note written. {len(items) - 1} left.\n"
                      f"Review:   git -C '{vault}' show HEAD\n"
                      f"Continue: distill.py --apply --commit --no-gate")
                return 0

    if not args.apply:
        print("To apply: distill.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
