#!/usr/bin/env python3
"""
Reuse guard for Claude Code.

Two entry points, one analysis:

  PreToolUse  Write|Edit|MultiEdit   the code is known before it hits disk.
  PostToolUse Bash                   the code is not knowable beforehand (cat
                                     heredoc, sed -i, python - <<PY ...), so
                                     look at what changed in the repo right
                                     after the command ran.

Measured on one real setup: 96% of file writes went through Bash, so a guard
that only listens to Write/Edit is blind to almost everything.

Operating rules:
  - Only speaks above a score threshold.
  - Never warns twice for the same symbol in one session.
  - Never blocks: it injects context, the model decides.
  - Never stalls: over budget, it exits silently.
  - Always fails silently.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# The engine lives next to this file, so the install is self-contained.
sys.path.insert(0, str(Path(__file__).resolve().parent))

T0 = time.time()
BUDGET = 2.5              # seconds; beyond this the edit stalls and the hook is a nuisance
MIN_SCORE = 7.0           # calibrated: exact name ~14, real kinship ~8-10
MAX_HITS = 4
MAX_SYMS = 6              # in a big file, the first symbols already give the theme
MAX_FILES = 5             # Bash: files changed by one command worth looking at
RECENT = 90               # Bash: seconds; older mtimes were not this command
STATE_TTL_DAYS = 7

# Everything written goes here, never inside the user's repository.
HOME_DIR = Path(os.environ.get("DUPE_GUARD_HOME")
                or (Path.home() / ".dupe-guard"))
STATE_DIR = HOME_DIR / "state"
CONV_DIR = HOME_DIR / "conventions"

CD = re.compile(r"(?:^|[;&|]\s*|\bcd\s+)cd\s+([^\s;&|]+)", re.M)


def out(event: str, text: str):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": text,
        }
    }))


def state_file(sid: str) -> Path:
    return STATE_DIR / f"{(sid or 'nosession')[:64]}.json"


def load_state(sid: str) -> dict:
    try:
        return json.loads(state_file(sid).read_text())
    except Exception:
        return {"warned": [], "conv": False}


def save_state(sid: str, st: dict):
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        # Atomic: parallel edits in one session must not leave a torn file.
        target = state_file(sid)
        tmp = target.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(st))
        os.replace(tmp, target)
        prune_state()
    except Exception:
        pass


def prune_state():
    """Drop state of sessions older than STATE_TTL_DAYS; it never expires otherwise."""
    cutoff = time.time() - STATE_TTL_DAYS * 86400
    for f in STATE_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def new_code(tool: str, ti: dict) -> str:
    """The text about to hit the disk."""
    if tool == "Write":
        return ti.get("content") or ""
    if tool == "Edit":
        return ti.get("new_string") or ""
    if tool == "MultiEdit":
        return "\n".join(e.get("new_string") or "" for e in ti.get("edits", []))
    return ""


def git(root, *args, timeout=2):
    try:
        r = subprocess.run(["git", *args], cwd=str(root), capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def roots_for(cmd: str, cwd: str, csearch):
    """The repos a Bash command may have touched: the cwd's, plus any `cd`."""
    seen, roots = set(), []
    cands = [cwd] + [os.path.expanduser(os.path.expandvars(p.strip("'\"")))
                     for p in CD.findall(cmd)]
    for c in cands[:4]:
        p = Path(c) if os.path.isabs(c) else Path(cwd) / c
        if not p.is_dir():
            continue
        r = csearch.repo_root(p)
        if (r / ".git").exists() and str(r) not in seen:
            seen.add(str(r))
            roots.append(r)
    return roots


def changed_code(root, csearch):
    """[(path, added_code, lang, existing_symbols)] for code files this command
    changed. Tracked: only the added lines, and the symbols HEAD already had.
    Untracked: the whole file, nothing pre-existing."""
    status = git(root, "status", "--porcelain", "--untracked-files=all")
    if not status:
        return []
    now = time.time()
    found = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code, rel = line[:2], line[3:].strip().strip('"')
        if " -> " in rel:
            rel = rel.split(" -> ", 1)[1]
        p = root / rel
        ext = os.path.splitext(rel)[1]
        lang = csearch.EXTS.get(ext)
        if not lang or csearch.SKIP_FILE.search(os.path.basename(rel)):
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if now - st.st_mtime > RECENT or st.st_size > csearch.MAX_BYTES:
            continue
        if code == "??":
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            found.append((str(p), text, lang, set()))
        else:
            diff = git(root, "diff", "-U0", "--no-color", "HEAD", "--", rel)
            added = "\n".join(l[1:] for l in diff.splitlines()
                              if l.startswith("+") and not l.startswith("+++"))
            head = git(root, "show", f"HEAD:{rel}")
            already = {y["n"] for y in csearch.extract_text(head, lang)} if head else set()
            found.append((str(p), added, lang, already))
        if len(found) >= MAX_FILES:
            break
    return found


def analyse(fpath, code, lang, already, root, st, warned, csearch, after):
    """Returns (parts, fresh_names, warned) for one file's new code."""
    if len(code) < 40:
        return [], [], warned
    syms = [y for y in csearch.extract_text(code, lang) if y["n"] not in already]
    if not syms:
        return [], [], warned

    data = csearch.load(root, max_age=90, budget=BUDGET - (time.time() - T0))
    try:
        rel_self = str(Path(fpath).resolve().relative_to(root))
    except Exception:
        rel_self = ""

    # Clone: the body is already in the repo under a different name. A name
    # query never finds this - handleCreditError and handleDebitError are the
    # same body and nothing relates them by name.
    clone_blocks = []
    try:
        idx = {}
        for rel, f in data.get("files", {}).items():
            for fp in f.get("p", []):
                idx.setdefault(fp["fp"], []).append((rel, fp["n"], fp["l"], fp["t"]))
        for nf in csearch.fingerprints(code, lang):
            # Same file is fine when the NAME differs: that is a clone inside
            # the file. Same file and same name is the function matching its
            # own indexed body, which is not news.
            same = [x for x in idx.get(nf["fp"], [])
                    if x[0] != rel_self or x[1] != nf["n"]]
            if not same or nf["fp"] in warned:
                continue
            where = "\n".join(f"  - `{r}:{l}` → `{n}`" for r, n, l, _ in same[:3])
            clone_blocks.append(
                f"**`{nf['n']}`** — this exact body ({nf['t']} tokens) already "
                f"exists under another name:\n{where}")
            warned = warned | {nf["fp"]}
    except Exception:
        pass

    blocks, fresh = [], []
    for sym in syms[:MAX_SYMS]:
        if time.time() - T0 > BUDGET:
            break
        name = sym["n"]
        if name in warned:
            continue
        hits = [h for h in csearch.search(data, name, MAX_HITS, rel_self)
                if h["score"] >= MIN_SCORE]
        if not hits:
            continue
        fresh.append(name)
        lines = [f"**`{name}`** ({sym['k']}) — something similar already exists:"]
        for h in hits:
            lines.append(f"  - `{h['path']}:{h['line']}` → `{h['sig']}`")
        blocks.append("\n".join(lines))

    parts = []
    where = f" in `{rel_self}`" if (after and rel_self) else ""
    if clone_blocks:
        parts.append(
            f"## Reuse: duplicated body{where}\n\n"
            + "\n\n".join(clone_blocks)
            + ("\n\n_This is not a similar name: it is the same code. You just "
               "wrote it; extract one function and parametrise the difference._"
               if after else
               "\n\n_This is not a similar name: it is the same code. Extract "
               "one function and parametrise the difference._")
        )
    if blocks:
        tail = ("_You just wrote this. Read the candidates above; if one fits, "
                "replace what you wrote with an import or an extension. If none "
                "fits, say in one line why not._" if after else
                "_Before writing: read the candidates above. If one fits, "
                "import or extend it instead of rewriting. If none fits, carry on — "
                "but say in one line why not._")
        parts.append(f"## Reuse: possible duplication{where}\n\n"
                     + "\n\n".join(blocks) + "\n\n" + tail)
    return parts, fresh, warned


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool = payload.get("tool_name") or ""
    event = payload.get("hook_event_name") or "PreToolUse"
    ti = payload.get("tool_input") or {}
    cwd = payload.get("cwd") or os.getcwd()

    import csearch

    # targets: [(path, code, lang, already_declared, root)]
    targets = []
    after = False
    if tool in ("Write", "Edit", "MultiEdit"):
        fpath = ti.get("file_path") or ""
        if not fpath:
            return 0
        lang = csearch.EXTS.get(os.path.splitext(fpath)[1])
        if not lang or csearch.SKIP_FILE.search(os.path.basename(fpath)):
            return 0
        code = new_code(tool, ti)
        # Skip symbols the target file already declares - otherwise editing a
        # function warns that the function exists, pointing at the line you're
        # editing.
        already = set()
        target = Path(fpath)
        if target.exists():
            try:
                already = {y["n"] for y in csearch.extract(target, lang)}
            except Exception:
                pass
        parent = target.parent if target.parent.exists() else Path(cwd)
        root = csearch.repo_root(parent)
        # Outside a git repo the fallback root is the file's own folder, and for
        # ~/Documents/x.py that means indexing all of ~/Documents. Not worth it.
        if not (root / ".git").exists():
            return 0
        targets.append((fpath, code, lang, already, root))
    elif tool == "Bash" and event == "PostToolUse":
        after = True
        for root in roots_for(ti.get("command") or "", cwd, csearch):
            for fpath, code, lang, already in changed_code(root, csearch):
                targets.append((fpath, code, lang, already, root))
            if len(targets) >= MAX_FILES:
                break
    else:
        return 0

    if not targets:
        return 0

    sid = payload.get("session_id") or ""
    st = load_state(sid)
    warned = set(st.get("warned", []))
    parts, fresh = [], []

    # Repo conventions: once per session, on the first code write.
    if not st.get("conv"):
        root = targets[0][4]
        conv = CONV_DIR / f"{root.name}.md"
        if conv.exists():
            try:
                body = conv.read_text().strip()
                if body:
                    parts.append(f"## Conventions for {root.name}\n\n{body}")
            except Exception:
                pass
        st["conv"] = True

    for fpath, code, lang, already, root in targets:
        if time.time() - T0 > BUDGET:
            break
        p, f, warned = analyse(fpath, code, lang, already, root, st, warned, csearch, after)
        parts += p
        fresh += f

    if parts:
        out(event, "\n\n".join(parts))
        st["warned"] = sorted(warned | set(fresh))
        save_state(sid, st)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
