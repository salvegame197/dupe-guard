#!/usr/bin/env python3
"""
proposals — the harness proposes changes to itself; a human approves.

Every proposal comes from objective evidence in a session, never opinion.
Since 2026-11-08 the page only receives the `stale` kind (a path a note
cites that no repo has, from brain/freshness.py). The two other kinds are
read, never written:

  - `convention` (the user corrected the model and the model admitted it)
    is not generated any more: in seven weeks 21 were proposed and none
    applied. The same evidence now becomes a draft line where the next
    session reads it, the repo's CLAUDE.md or the native memory
    (brain/learned.py). Lines already on the page can still be approved
    with `--apply`.

The `stopword` category (a term that matched the title of an injected page
the model did not open) is no longer generated, since 2026-11-08: the model
never opens the page (0 in 1,145 injections, measured), so EVERY injection
became a proposal: 486 pending, 4 approved in seven weeks. The recall line
("N injections, M with a page read") is still written as information, and
`--apply` still honours a `stopword` line someone writes by hand.

Nothing applies itself. Proposals go to a page in the vault with checkboxes:
`[x]` + `proposals.py --apply` applies; `[-]` rejects. When a category's
approval rate stays high for weeks, automating that one category can be
discussed.

Usage:
  proposals.py --generate <transcript.jsonl> <session_id> [cwd] [--record]
  proposals.py --list
  proposals.py --apply
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import (DATA, HOME_DIR, LABELS, RECALL_MARKS, fold,       # noqa: E402
                        injected_context, is_dataless, labels, load_config)

MANUAL_STOPWORDS = DATA / "recall-stopwords.json"
CONV_DIR = HOME_DIR / "conventions"

TITLE = re.compile(r"^\*\*(.+?)\*\* \(", re.M)

# The user correcting the model, written the way people actually write.
CORRECTION = {
    "en": (r"^\s*no\b|\b(wrong|that's not it|that is not it|not what i (asked|said|meant)"
           r"|why did you (say|write|do)|you (said|wrote|made up) (that|this)"
           r"|i (said|asked|told you)|i didn't (say|ask)|that doesn't exist|that's not true"
           r"|actually,? i)\b"),
    "pt": (r"^\s*(nao|não)\b|\b(errado|errou|nao era isso|não era isso|nao foi isso|não foi isso"
           r"|porque tu (fala|falou|disse|fez)|por que (tu|voce|você) (fala|falou|disse|fez)"
           r"|tu (falou|disse|fez) (que|isso)|eu (disse|pedi|falei) (que|pra|para)"
           r"|nao (quero|era|e) (isso|assim)|não (quero|era|é) (isso|assim)|esquece isso"
           r"|na verdade eu|isso nao (existe|esta|ta)|isso não (existe|está|tá))\b"),
}
# The model admitting it. Without this, a leading "no" may just answer a question.
ADMISSION = {
    "en": (r"\b(i made (that|it) up|i was wrong|my mistake|you're right|you are right"
           r"|i got (that|it) wrong|i invented|i confused|that was wrong of me|i stand corrected)\b"),
    "pt": (r"\b(fui eu que|eu inventei|eu errei|errei|meu erro|corrigido|voce tem razao|você tem razão"
           r"|tem razao|tem razão|eu que (errei|inventei|confundi)|foi erro meu"
           r"|me enganei|eu confundi|retiro o que)\b"),
}


def events(path):
    """(ts, kind, data) in transcript order: prompt, assistant text, tool_use,
    recall injection."""
    out = []
    with open(path, "rb") as f:
        for raw in f:
            if not any(k in raw for k in (b"hook_success", b"hook_additional_context", b'"tool_use"', b'"assistant"', b'"user"')):
                continue
            try:
                d = json.loads(raw)
            except ValueError:
                continue
            ts, t = d.get("timestamp") or "", d.get("type")
            if t == "attachment":
                a = d.get("attachment") or {}
                if not (a.get("hookName") or "").startswith("UserPromptSubmit"):
                    continue
                c = injected_context(a)
                if c and any(m in c for m in RECALL_MARKS):
                    out.append((ts, "recall", c))
            elif t == "assistant":
                for b in (d.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        out.append((ts, "tool", {"name": b.get("name"), "path": inp.get("file_path") or ""}))
                    elif b.get("type") == "text" and b.get("text"):
                        out.append((ts, "text", b["text"]))
            elif t == "user":
                c = (d.get("message") or {}).get("content")
                if isinstance(c, str):
                    out.append((ts, "prompt", c))
                elif isinstance(c, list) and c and isinstance(c[0], dict) and c[0].get("type") == "text":
                    out.append((ts, "prompt", c[0].get("text") or ""))
    return out


def git_root_name(cwd):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or None,
                           capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return os.path.basename(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def generate(transcript, sid, cwd, cfg):
    L = labels(cfg)
    ev = events(transcript)
    props, info = [], []

    # --- recall: how many injections, how many followed by a Read of the
    # page. Information only; it no longer proposes stopwords (see the
    # module docstring).
    injections = read = 0
    for i, (ts, k, c) in enumerate(ev):
        if k != "recall":
            continue
        injections += 1
        titles = TITLE.findall(c)
        opened = set()
        for _, k2, d2 in ev[i + 1:]:
            if k2 == "prompt":
                break
            if k2 == "tool" and d2["name"] == "Read":
                opened.add(fold(os.path.splitext(os.path.basename(d2["path"]))[0]))
        if any(fold(t) in opened for t in titles):
            read += 1
    if injections:
        info.append(L["proposal_injections"].format(inj=injections, read=read))

    # Admitted corrections no longer become `convention` proposals here:
    # brain/learned.py turns them into a line in the repo's CLAUDE.md or
    # the native memory at the same SessionEnd. CORRECTION and ADMISSION
    # above stay as the shared vocabulary (learned.py, hit_rate.py).
    return props, info


def page_path(cfg) -> Path:
    return cfg["vault"] / cfg["proposals_page"]


def record(props, info, sid, cfg, when=None):
    if not props and not info:
        return 0
    L = labels(cfg)
    page = page_path(cfg)
    day = (when or datetime.now()).strftime("%Y-%m-%d")
    short = (sid or "nosession")[:8]
    if is_dataless(page):
        return 0            # evicted from disk: reading would block the hook
    if page.exists() and re.search(rf"^## .* · \w+ {re.escape(short)} ",
                                   page.read_text(encoding="utf-8"), re.M):
        return 0
    if not page.exists():
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(
            f"---\ntags: [harness, proposals]\n---\n\n# {page.stem}\n\n"
            "The harness proposes changes to itself from objective evidence in each\n"
            "session. **Nothing applies itself.** Decide in chat, or mark `[x]` to\n"
            "approve and `[-]` to reject; then ask Claude to apply them (the dupe-guard\n"
            "memory skill runs `brain/proposals.py --apply`).\n"
            "Kinds: `convention` (a rule for the repo, injected by the guard), `stale`\n"
            "(a path a note cites that no repo has); a hand-written `stopword` line\n"
            "(recall stops searching for that word) is applied too.\n\n",
            encoding="utf-8")
    n = len(props)
    lines = [f"## {day} · {L['proposal_session']} {short} "
             f"({n} {L['proposal_word']}{'s' if n != 1 else ''})", ""]
    for i, p in enumerate(props, 1):
        lines.append(f"- [ ] `{p['kind'][:2]}-{short}-{i}` **{p['kind']}** `{p['target']}` — {p['text']}")
    for i in info:
        lines.append(f"- ℹ️ {i}")
    lines.append("")
    with open(page, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return n


def safe_target(target: str) -> bool:
    """A target is a repo basename or a note name, never a path. The page it
    comes from lives in the vault and any app can edit it; `../x` or `a/b`
    would otherwise be written to or searched for outside CONV_DIR."""
    return bool(target) and not any(c in target for c in ("/", "\\", "..")) and target not in (".",)


ITEM = re.compile(r"^- \[(?P<m>[ x\-])\] `(?P<id>[\w-]+)` \*\*(?P<kind>\w+)\*\* "
                  r"`(?P<target>[^`]+)` — (?P<text>.+)$")
APPLIED = tuple(f"✅ {l['applied']}" for l in LABELS.values())


def items(cfg):
    page = page_path(cfg)
    if not page.exists() or is_dataless(page):
        return []
    out = []
    for ln in page.read_text(encoding="utf-8").splitlines():
        m = ITEM.match(ln)
        if m:
            d = m.groupdict()
            d["line"] = ln
            d["applied"] = any(a in ln for a in APPLIED)
            out.append(d)
    return out


def pending(cfg):
    return [i for i in items(cfg) if i["m"] == " "]


def apply(cfg):
    L = labels(cfg)
    page = page_path(cfg)
    txt = page.read_text(encoding="utf-8") if page.exists() else ""
    today = datetime.now().strftime("%Y-%m-%d")
    n = 0
    for it in items(cfg):
        if it["m"] != "x" or it["applied"]:
            continue
        if it["kind"] == "stopword":
            try:
                cur = json.loads(MANUAL_STOPWORDS.read_text()) if MANUAL_STOPWORDS.exists() else []
            except (OSError, ValueError):
                cur = []
            if it["target"] not in cur:
                cur.append(it["target"])
                MANUAL_STOPWORDS.parent.mkdir(parents=True, exist_ok=True)
                MANUAL_STOPWORDS.write_text(json.dumps(sorted(cur), ensure_ascii=False, indent=0))
        elif it["kind"] in ("convention", "convencao"):
            if not safe_target(it["target"]):
                print(f"skipped {it['id']}: target {it['target']!r} is not a plain name")
                continue
            CONV_DIR.mkdir(parents=True, exist_ok=True)
            target = CONV_DIR / f"{it['target']}.md"
            if not target.exists():
                target.write_text("## Confirmed patterns\n\n## Do not do here\n\n## Superseded\n",
                                  encoding="utf-8")
            said = it["text"].split(" — ")[0]
            with open(target, "a", encoding="utf-8") as f:
                f.write(f"\n- **Rule (from a correction on {today}, review the wording)**: {said} <!-- votes:2 -->\n")
        elif it["kind"] == "stale":
            if not mark_stale(cfg, it, today):
                continue
        else:
            continue
        txt = txt.replace(it["line"], it["line"] + f" ✅ {L['applied']} {today}", 1)
        n += 1
        print(f"applied {it['id']} ({it['kind']} {it['target']})")
    if n:
        page.write_text(txt, encoding="utf-8")
    print(f"{n} applied")
    return 0


STALE_TEXT = re.compile(r"`([^`]+)` .*?«(.+?)»")


def mark_stale(cfg, it, today) -> bool:
    """Mark the section that names a path the repo no longer has. The mark
    goes right under the heading; nothing is deleted."""
    m = STALE_TEXT.search(it["text"])
    if not m or not safe_target(it["target"]):
        return False
    path, section = m.group(1), m.group(2)
    found = [p for p in cfg["vault"].rglob(f"{it['target']}.md") if ".obsidian" not in p.parts]
    if len(found) != 1:
        return False
    note = found[0]
    txt = note.read_text(encoding="utf-8")
    L = labels(cfg)
    line = f"\n\n> ⚠️ **{L['superseded_on']} {today}**: `{path}` {L['no_longer_exists']}"
    if section == "(top)":
        h = re.search(r"^# .+$", txt, re.M)
    else:
        h = re.search(rf"^#{{2,3}} {re.escape(section)}\s*$", txt, re.M)
    if not h or f"`{path}` {L['no_longer_exists']}" in txt:
        return False
    note.write_text(txt[:h.end()] + line + txt[h.end():], encoding="utf-8")
    return True


def main():
    cfg = load_config()
    a = sys.argv[1:]
    if a[:1] == ["--generate"] and len(a) >= 3:
        cwd = a[3] if len(a) > 3 and not a[3].startswith("--") else ""
        props, info = generate(a[1], a[2], cwd, cfg)
        for p in props:
            print(f"  {p['kind']:<10} {p['target']:<18} {p['text'][:90]}")
        for i in info:
            print(f"  ℹ️  {i}")
        if "--record" in a:
            print(f"recorded: {record(props, info, a[2], cfg)}")
        return 0
    if a[:1] == ["--apply"]:
        return apply(cfg)
    p = pending(cfg)
    print(f"{len(p)} pending")
    for it in p:
        print(f"  `{it['id']}` {it['kind']} {it['target']} — {it['text'][:80]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
