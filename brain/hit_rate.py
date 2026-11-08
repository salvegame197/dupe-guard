#!/usr/bin/env python3
"""
hit_rate — do the hooks help, or do they just spend context?

Reads the transcripts and, for each hook injection, looks at what happened
next. It touches no hook: their output is already recorded in the transcript,
so measuring is entirely offline.

The ruler is the OUTCOME, not whether the page was opened (it never is: 0 in
1,145 injections, measured 2026-11-08). Per recall injection, and per page in
it:

  (a) lead touched    a path or symbol in backticks in the injected snippet
                      was used by Read/Edit/Write/Bash/Grep/Glob in the same
                      turn (the cleanest signal there is)
  (b) candidate edited (guard) the model edited the file the warning pointed
                      at instead of creating a new one
  (c) corrected       the next HUMAN prompt matches the correction regex of
                      brain/proposals.py, shown next to the base rate of the
                      same repo (every human prompt, injected or not): the
                      injection is suspect only above the base
  (d) ksearch         the model ran ksearch.py itself in that turn
  (e) rule written    the session wrote a native memory note or a CLAUDE.md
  opened, cited       the page was read / its title appears in the answer;
                      kept as columns, no longer the ruler

`--strength` writes brain/recall-strength.json from (a) and (c); the formula
is in brain/recall.py:strength_factor. The RECALL LOG block reads
brain/recall-log.jsonl instead: decisions by kind, session mode and the
injected pages by vault folder.

Usage:
  hit_rate.py                  every transcript
  hit_rate.py <session.jsonl>  one
  hit_rate.py --json
  hit_rate.py --strength       rewrite the strength table and show its spread
  hit_rate.py --top 50         rows per table (default 30)
"""
import glob
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import (DATA, RECALL_MARKS, WORKER_DIR, injected_context,     # noqa: E402
                        load_config, project_folder_name)

WINDOW = 6          # following tool calls that count as a "reaction" to the guard

GUARD_MARKS = ("Reuse: possible duplication", "Reuse: duplicated body",
               "Reuso: possivel duplicacao", "Reuso: corpo duplicado")
CAND = re.compile(r"`([^`:]+):(\d+)`")           # `path:line`
SYM = re.compile(r"\*\*`([^`]+)`\*\*")            # **`name`**
TITLE = re.compile(r"^\*\*(.+?)\*\* \(", re.M)    # **title** (origin
LEAD = re.compile(r"`([^`\n]{4,80})`")            # `path/or/symbol` inside a snippet
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
# tool_use input fields a lead can show up in; Write's `content` is left out
# (a page pasted whole would "touch" every lead in it)
ARG_FIELDS = ("file_path", "notebook_path", "command", "pattern", "path", "glob",
              "old_string", "new_string")
MIN_TITLE_CITE = 5  # a title shorter than this is a common word


def correction_regex(cfg):
    from brain.proposals import CORRECTION
    return re.compile("|".join(CORRECTION[l] for l in cfg["languages"] if l in CORRECTION), re.I)


def is_human(text: str) -> bool:
    """Typed by the person: not a notification, system envelope, image or
    pasted skill. The recall's length rule is NOT applied: "não, errado"
    is too short to search for and exactly the prompt this measures."""
    from brain.recall import non_human
    return non_human(text) in ("", "short")


def is_learned(path: str, transcripts=None) -> bool:
    from brain.session_log import is_learned as learned
    return learned(path, transcripts)


def events(path):
    """(ts, kind, data) for one transcript, only what matters here. The first
    event is ("", "cwd", {"cwd": ...}) when the transcript records one."""
    out, seen = [], set()
    cwd_seen = False
    with open(path, "rb") as f:
        for raw in f:
            if not any(k in raw for k in (b"hook_success", b"hook_additional_context",
                                          b'"tool_use"', b'"assistant"', b'"user"')):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            if not cwd_seen and d.get("cwd"):
                cwd_seen = True
                out.insert(0, ("", "cwd", {"cwd": d["cwd"]}))
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
                                                 "cmd": inp.get("command") or "",
                                                 "args": " ".join(str(inp[k]) for k in ARG_FIELDS
                                                                  if isinstance(inp.get(k), str))}))
                    elif b.get("type") == "text":
                        out.append((ts, "text", {"text": b.get("text") or ""}))
            elif t == "user":
                if d.get("isMeta"):
                    continue
                c = (d.get("message") or {}).get("content")
                text = None
                if isinstance(c, str):
                    text = c
                elif isinstance(c, list) and c and isinstance(c[0], dict) and c[0].get("type") == "text":
                    text = c[0].get("text") or ""
                if text is not None:
                    out.append((ts, "prompt", {"text": text, "human": is_human(text)}))
    return out


# ─── leads ───────────────────────────────────────────────────────────────

PLAIN_WORD = re.compile(r"^[A-Za-z]+$")
IDENTIFIER_MARK = re.compile(r"[/._$:>()\-]|[a-z][A-Z]")   # path, symbol, snake/kebab/camel case


def leads_in(snippet: str):
    """Backtick spans in an injected snippet that name something a tool can
    touch: a path (`src/payments.js:12`, `utils/PrintDedup`) or a symbol
    (`handleCreditError`, `order_consumes`, `$request->producer_id`). A
    plain word in backticks (`preview`, `production`, `null`, `order`) is
    not a lead: it matches any Bash command by accident, and those were
    half of the false hits when every span counted."""
    out = []
    for m in LEAD.findall(snippet):
        lead = m.strip().strip("()[]{}.,;")
        if len(lead) < 4 or not re.search(r"[A-Za-z]", lead):
            continue
        if PLAIN_WORD.match(lead) or not IDENTIFIER_MARK.search(lead):
            continue
        if " " in lead and len(lead) > 40:
            continue
        out.append(lead)
    return out


def lead_key(lead: str) -> str:
    """What to look for in a tool call: the basename for a path (the model
    opens `/abs/path/to/file.js`, the snippet said `src/file.js:12`), the
    text itself for a symbol."""
    base = lead.split(":")[0] if re.search(r":\d+$", lead) else lead
    if "/" in base:
        base = base.rstrip("/").rsplit("/", 1)[-1]
    return base


def touched(leads, tools, cwd: str = "") -> bool:
    """Some tool call in the turn used one of the leads. A key that is
    already part of the session's cwd (the repo's own name) proves nothing:
    every `cd` would match it."""
    keys = [lead_key(l) for l in leads]
    # the basename of `config/production` is a plain word again: same rule
    keys = [k for k in keys if len(k) >= 4 and not PLAIN_WORD.match(k) and not (cwd and k in cwd)]
    return any(k in t["args"] for k in keys for t in tools)


def pages_of(text: str):
    """[(title, [leads])] for the blocks of one injection: a `**title** (`
    line followed by `  - snippet` lines. The footer (ksearch command in
    backticks) is not under any title and is ignored."""
    blocks = []
    for line in text.splitlines():
        m = TITLE.match(line)
        if m:
            blocks.append((m.group(1).strip(), []))
        elif line.startswith("  - ") and blocks:
            blocks[-1][1].extend(leads_in(line[4:]))
    return blocks


# ─── per transcript ─────────────────────────────────────────────────────

def turn_after(ev, i):
    """Tool calls and assistant text until the next prompt."""
    tools, texts = [], []
    for _, k2, d2 in ev[i + 1:]:
        if k2 == "prompt":
            break
        if k2 == "tool":
            tools.append(d2)
        elif k2 == "text":
            texts.append(d2["text"])
    return tools, texts


def next_human_prompt(ev, i):
    for _, k2, d2 in ev[i + 1:]:
        if k2 == "prompt" and d2.get("human"):
            return d2["text"]
    return None


def analyse(path, acc, correction, transcripts=None):
    """Fill acc with this transcript's guard reactions, recall outcomes and
    human prompts. acc["records"] gets one row per (injection, page);
    acc["injections"] one per injection; acc["prompts"][repo] the human
    prompts and corrections for the base rate."""
    ev = events(path)
    cwd = next((d["cwd"] for _, k, d in ev if k == "cwd"), "")
    repo = Path(cwd).name if cwd else "?"
    learned = any(k == "tool" and d["name"] in EDITS and is_learned(d["path"], transcripts) for _, k, d in ev)
    for _, k, d in ev:
        if k == "prompt" and d.get("human"):
            acc["prompts"][repo]["human"] += 1
            acc["prompts"][repo]["corrected"] += bool(correction.search(d["text"]))
    for i, (ts, kind, d) in enumerate(ev):
        if kind == "guard":
            c = d["text"]
            cands = {p for p, _ in CAND.findall(c)}
            syms = set(SYM.findall(c))
            # the guarded Write/Edit: the tool_use with that id, for its file
            target = next((e[2]["path"] for e in ev if e[1] == "tool" and e[2]["id"] == d["tool"]), "")
            read = revised = cited = edited_cand = False
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
                if d2["name"] in EDITS and target and p == target:
                    revised = True
                if d2["name"] in EDITS and p and any(p.endswith(cd) for cd in cands) and p != target:
                    edited_cand = True          # (b) went to the existing one
            g = acc["guard"]
            g["warnings"] += 1
            g["read"] += read
            g["revised"] += revised
            g["cited"] += cited
            g["edited_candidate"] += edited_cand
            g["ignored"] += not (read or revised or cited or edited_cand)
            g["chars"] += len(c)
        elif kind == "recall":
            c = d["text"]
            tools, texts = turn_after(ev, i)
            answer = " ".join(texts).lower()
            nxt = next_human_prompt(ev, i)
            corrected = bool(nxt is not None and correction.search(nxt))
            deeper = any(t["name"] == "Bash" and "ksearch" in t["cmd"] for t in tools)
            opened = {os.path.splitext(os.path.basename(t["path"]))[0].lower()
                      for t in tools if t["name"] == "Read"}
            rows = []
            for title, leads in pages_of(c):
                rows.append({"title": title, "repo": repo, "lead": bool(leads),
                             "touched": bool(leads) and touched(leads, tools, cwd),
                             "opened": title.lower() in opened,
                             "cited": len(title) >= MIN_TITLE_CITE and title.lower() in answer,
                             "corrected": corrected, "deeper": deeper, "learned": learned})
            acc["records"].extend(rows)
            acc["injections"].append({
                "repo": repo, "pages": len(rows), "chars": len(c),
                "lead": any(r["lead"] for r in rows), "touched": any(r["touched"] for r in rows),
                "opened": any(r["opened"] for r in rows), "cited": any(r["cited"] for r in rows),
                "corrected": corrected, "deeper": deeper, "learned": learned,
                "has_next": nxt is not None})


def new_acc():
    return {"guard": Counter(), "records": [], "injections": [],
            "prompts": defaultdict(Counter)}


def base_rates(acc):
    """repo -> correction rate over every human prompt of that repo, and
    the overall one under '*'."""
    out = {}
    total = Counter()
    for repo, c in acc["prompts"].items():
        out[repo] = c["corrected"] / c["human"] if c["human"] else 0.0
        total.update(c)
    out["*"] = total["corrected"] / total["human"] if total["human"] else 0.0
    return out


SIGNALS = ("lead", "touched", "opened", "cited", "corrected", "deeper", "learned")


def by_key(records, key, base):
    """Aggregate rows by `key` (title or repo): counts per signal plus the
    expected corrections (sum of the base rate of each row's repo)."""
    table = {}
    for r in records:
        s = table.setdefault(r[key], Counter())
        s["injected"] += 1
        for sig in SIGNALS:
            s[sig] += bool(r[sig])
        s["expected"] += base.get(r["repo"], base["*"])
    return table


def summary(acc):
    inj = acc["injections"]
    base = base_rates(acc)
    n = len(inj)
    with_lead = sum(i["lead"] for i in inj)
    with_next = sum(i["has_next"] for i in inj)
    return {
        "injections": n, "pages": sum(i["pages"] for i in inj),
        "chars": sum(i["chars"] for i in inj),
        "with_lead": with_lead,
        "touched": sum(i["touched"] for i in inj),
        "opened": sum(i["opened"] for i in inj),
        "cited": sum(i["cited"] for i in inj),
        "with_next": with_next,
        "corrected": sum(i["corrected"] for i in inj),
        "deeper": sum(i["deeper"] for i in inj),
        "learned": sum(i["learned"] for i in inj),
        "base": base["*"],
        "human_prompts": sum(c["human"] for c in acc["prompts"].values()),
        "human_corrections": sum(c["corrected"] for c in acc["prompts"].values()),
    }


# ─── strength ───────────────────────────────────────────────────────────

def strength(files, cfg=None):
    """{title: {injected, touched, corrected, expected, opened, lead}} from
    every recall injection: the evidence recall.strength_factor turns into a
    multiplier. `expected` is the correction count the page's injections
    would show if they only followed the base rate of their repos."""
    cfg = cfg or load_config()
    acc = new_acc()
    correction = correction_regex(cfg)
    for path in files:
        try:
            analyse(path, acc, correction, cfg["transcripts"])
        except (OSError, ValueError):
            continue
    base = base_rates(acc)
    table = {}
    for title, s in by_key(acc["records"], "title", base).items():
        table[title] = {"injected": s["injected"], "touched": s["touched"], "lead": s["lead"],
                        "corrected": s["corrected"], "expected": round(s["expected"], 2),
                        "opened": s["opened"]}
    return table


def write_strength(files, cfg=None):
    """Keyed by index id, so recall can look it up without a title search."""
    from brain import ksearch
    by_title = {}
    if ksearch.INDEX.exists():
        for doc in ksearch.load_index():
            by_title.setdefault(doc["title"], doc["id"])
            by_title.setdefault(doc["title"][:60], doc["id"])
    table = strength(files, cfg)
    out = {by_title[t]: s for t, s in table.items() if t in by_title}
    ksearch.DATA.mkdir(parents=True, exist_ok=True)
    (ksearch.DATA / "recall-strength.json").write_text(json.dumps(out, ensure_ascii=False))
    return out


BANDS = (("< 0.8", lambda f: f < 0.8), ("0.8", lambda f: 0.8 <= f < 1.0),
         ("1.0", lambda f: f == 1.0), ("1.0-1.3", lambda f: 1.0 < f <= 1.3),
         ("> 1.3", lambda f: f > 1.3))


def strength_spread(out):
    """(min, median, max, {band: pages}) of the factors the table yields."""
    from brain.recall import strength_factor
    factors = [strength_factor(k, out) for k in out]
    if not factors:
        return 0.0, 0.0, 0.0, {b: 0 for b, _ in BANDS}
    bands = {b: sum(1 for f in factors if test(f)) for b, test in BANDS}
    return min(factors), statistics.median(factors), max(factors), bands


# ─── report ─────────────────────────────────────────────────────────────

def pct(a, b):
    return f"{100 * a / b:5.1f}%" if b else "   —  "


def title_folders():
    """title -> set of top-level vault folders (or 'transcript' / 'memory/')
    the index knows that title under. Titles are not unique; the caller
    decides what to do with an ambiguous one."""
    from brain import ksearch
    out = {}
    if not ksearch.INDEX.exists():
        return out
    for doc in ksearch.load_index():
        if doc["src"] == "vault":
            folder = doc["id"].split("/")[0] + "/" if "/" in doc["id"] else "vault/"
        elif doc["src"] == "memory":
            folder = "memory/"
        else:
            folder = "transcript"
        out.setdefault(doc["title"], set()).add(folder)
        out.setdefault(doc["title"][:60], set()).add(folder)    # the log keeps 60 chars
    return out


def folder_of(title, ids_hint, by_title):
    """The top folder an injected title came from. A logged index id
    (written since 2026-11-08) settles it; before that, the title is
    looked up in the index and counts only when every page with that
    title sits in the same folder."""
    if ids_hint:
        return ids_hint.split("/")[0] + "/" if "/" in ids_hint else "transcript"
    f = by_title.get(title) or by_title.get(title[:60]) or set()
    return next(iter(f)) if len(f) == 1 else "?"


def log_report(log_path: Path):
    """What the decision log says: how many prompts were skipped as not
    human, how many were searched, and where the injected pages came from."""
    decisions, modes, folders = Counter(), Counter(), Counter()
    items = 0
    if not log_path.exists():
        return decisions, modes, folders, items
    by_title = title_folders()
    with log_path.open(encoding="utf-8") as f:
        for raw in f:
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            decisions[d.get("decision") or "?"] += 1
            if d.get("mode"):
                modes[d["mode"]] += 1
            ids = d.get("ids") or []
            for i, (title, _score) in enumerate(d.get("picked") or []):
                items += 1
                folders[folder_of(title, ids[i] if i < len(ids) else "", by_title)] += 1
    return decisions, modes, folders, items


def print_table(rows, key_label, base, top, with_base=True):
    head = f"  {'inj':>4} {'lead':>4} {'touch':>5} {'open':>4} {'cite':>4} {'corr':>4} "
    head += f"{'base':>6} " if with_base else ""
    head += f"{'ks':>3} {'rule':>4}   {key_label}"
    print(head)
    for name, s in sorted(rows.items(), key=lambda kv: (-kv[1]["injected"], kv[0]))[:top]:
        exp = f"{100 * s['expected'] / s['injected']:5.1f}%" if s["injected"] else "    — "
        line = (f"  {s['injected']:>4} {s['lead']:>4} {s['touched']:>5} {s['opened']:>4} "
                f"{s['cited']:>4} {s['corrected']:>4} ")
        line += f"{exp:>6} " if with_base else ""
        line += f"{s['deeper']:>3} {s['learned']:>4}   {name[:70]}"
        print(line)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--") and not a.isdigit()]
    top = 30
    if "--top" in sys.argv:
        try:
            top = int(sys.argv[sys.argv.index("--top") + 1])
        except (IndexError, ValueError):
            pass
    cfg = load_config()
    files = args or sorted(f for f in glob.glob(str(cfg["transcripts"] / "*" / "*.jsonl"))
                           # the brain's own `claude -p` children are not sessions
                           if Path(f).parent.name != project_folder_name(WORKER_DIR))
    if "--strength" in sys.argv:
        out = write_strength(files, cfg)
        lo, med, hi, bands = strength_spread(out)
        up = sum(1 for s in out.values() if s["touched"])
        print(f"strength table: {len(out)} pages, {up} with a lead touched; "
              f"factor min {lo:.2f}, median {med:.2f}, max {hi:.2f}; "
              + ", ".join(f"{b}: {n}" for b, n in bands.items()))
        return 0
    acc = new_acc()
    correction = correction_regex(cfg)
    for f in files:
        try:
            analyse(f, acc, correction, cfg["transcripts"])
        except (OSError, ValueError):
            continue
    base = base_rates(acc)
    s = summary(acc)
    pages = by_key(acc["records"], "title", base)
    repos = by_key(acc["records"], "repo", base)
    per_repo_inj = Counter(i["repo"] for i in acc["injections"])
    if "--json" in sys.argv:
        print(json.dumps({"transcripts": len(files), "guard": acc["guard"], "recall": s,
                          "base_rates": base,
                          "pages": {k: dict(v) for k, v in pages.items()},
                          "repos": {k: dict(v, injections=per_repo_inj[k],
                                            human=acc["prompts"][k]["human"]) for k, v in repos.items()}},
                         ensure_ascii=False))
        return 0
    g = acc["guard"]
    print(f"transcripts analysed: {len(files)}\n")
    print("GUARD")
    print(f"  warnings               {g['warnings']:>6}   ~{g['chars'] // 4:,} tokens injected")
    print(f"  read the candidate     {g['read']:>6}   {pct(g['read'], g['warnings'])}")
    print(f"  edited the candidate   {g['edited_candidate']:>6}   {pct(g['edited_candidate'], g['warnings'])}   (b)")
    print(f"  edited the file again  {g['revised']:>6}   {pct(g['revised'], g['warnings'])}")
    print(f"  cited the candidate    {g['cited']:>6}   {pct(g['cited'], g['warnings'])}")
    print(f"  ignored (none of it)   {g['ignored']:>6}   {pct(g['ignored'], g['warnings'])}")
    print()
    n = s["injections"]
    print("RECALL (outcome per injection)")
    print(f"  injections             {n:>6}   ~{s['chars'] // 4:,} tokens injected")
    print(f"  pages suggested        {s['pages']:>6}")
    print(f"  with a lead in `code`  {s['with_lead']:>6}   {pct(s['with_lead'], n)}")
    print(f"  lead touched           {s['touched']:>6}   {pct(s['touched'], s['with_lead'])} of those with a lead   (a)")
    print(f"  page opened            {s['opened']:>6}   {pct(s['opened'], n)}")
    print(f"  title cited in answer  {s['cited']:>6}   {pct(s['cited'], n)}")
    print(f"  next prompt corrected  {s['corrected']:>6}   {pct(s['corrected'], s['with_next'])} of {s['with_next']} with a next prompt;"
          f" base {pct(s['human_corrections'], s['human_prompts'])} ({s['human_corrections']} of {s['human_prompts']} human prompts)   (c)")
    print(f"  ksearch in the turn    {s['deeper']:>6}   {pct(s['deeper'], n)}   (d)")
    print(f"  rule written (session) {s['learned']:>6}   {pct(s['learned'], n)}   (e)")
    if pages:
        print(f"\nBY PAGE (top {min(top, len(pages))} of {len(pages)}, by injections; base = expected corrections from the repos' rates)")
        print_table(pages, "page", base, top)
    if repos:
        print(f"\nBY REPO ({len(repos)}; corr = injections followed by a correction, base = rate over the repo's human prompts)")
        rows = {}
        for repo, c in acc["prompts"].items():
            r = repos.get(repo, Counter())
            inj = per_repo_inj[repo]
            rows[f"{repo}  ({c['human']} prompts)"] = Counter(
                injected=inj, lead=sum(i["lead"] for i in acc["injections"] if i["repo"] == repo),
                touched=sum(i["touched"] for i in acc["injections"] if i["repo"] == repo),
                opened=sum(i["opened"] for i in acc["injections"] if i["repo"] == repo),
                cited=sum(i["cited"] for i in acc["injections"] if i["repo"] == repo),
                corrected=sum(i["corrected"] for i in acc["injections"] if i["repo"] == repo),
                expected=base[repo] * inj,
                deeper=sum(i["deeper"] for i in acc["injections"] if i["repo"] == repo),
                learned=sum(i["learned"] for i in acc["injections"] if i["repo"] == repo))
        print_table(rows, "repo", base, top)
    if not args:
        decisions, modes, folders, items = log_report(DATA / "recall-log.jsonl")
        total = sum(decisions.values())
        if total:
            print()
            print(f"RECALL LOG ({total} decisions)")
            for k in ("skip:non-human", "silent", "inject"):
                print(f"  {k:<22} {decisions[k]:>6}   {pct(decisions[k], total)}")
            if modes:
                print("  mode                   " + ", ".join(f"{k} {v}" for k, v in modes.most_common()))
            print(f"  pages injected         {items:>6}   by vault folder:")
            for folder, n in folders.most_common(8):
                print(f"    {folder:<20} {n:>6}   {pct(n, items)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
