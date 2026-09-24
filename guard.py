#!/usr/bin/env python3
"""
PreToolUse hook (Write|Edit|MultiEdit): reuse guard.

Before new code reaches disk, search the repo index for an equivalent: by
symbol name, and by body fingerprint for clones written under another name.

Operating rules:
  - Only speaks above a score threshold.
  - Never warns twice for the same symbol in one session.
  - Never blocks: it injects context, the model decides.
  - Never stalls: over budget, it exits silently.
  - Always fails silently.
"""

import json
import os
import sys
import time
from pathlib import Path

# The engine lives next to this file, so the install is self-contained.
sys.path.insert(0, str(Path(__file__).resolve().parent))

T0 = time.time()
BUDGET = 2.5              # segundos: acima disso a edicao trava e vira estorvo
MIN_SCORE = 7.0           # calibrated: exact name ~14, real kinship ~8-10
MAX_HITS = 4
MAX_SYMS = 6              # in a big file, the first symbols already give the theme

# Everything written goes here, never inside the user's repository.
HOME_DIR = Path(os.environ.get("DUPE_GUARD_HOME")
                or (Path.home() / ".dupe-guard"))
STATE_DIR = HOME_DIR / "state"
CONV_DIR = HOME_DIR / "conventions"


def out(text: str):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
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


STATE_TTL_DAYS = 7


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


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0

    tool = payload.get("tool_name") or ""
    if tool not in ("Write", "Edit", "MultiEdit"):
        return 0

    ti = payload.get("tool_input") or {}
    fpath = ti.get("file_path") or ""
    if not fpath:
        return 0

    import csearch

    ext = os.path.splitext(fpath)[1]
    lang = csearch.EXTS.get(ext)
    if not lang or csearch.SKIP_FILE.search(os.path.basename(fpath)):
        return 0

    code = new_code(tool, ti)
    if len(code) < 40:
        return 0

    syms = csearch.extract_text(code, lang)
    if not syms:
        return 0          # declares nothing new: nothing to duplicate

    # Skip symbols the target file already declares - otherwise editing a
    # function warns that the function exists, pointing at the line you're
    # editing.
    target = Path(fpath)
    if target.exists():
        try:
            already = {y["n"] for y in csearch.extract(target, lang)}
            syms = [y for y in syms if y["n"] not in already]
        except Exception:
            pass
    if not syms:
        return 0

    sid = payload.get("session_id") or ""
    st = load_state(sid)
    warned = set(st.get("warned", []))

    cwd = payload.get("cwd") or os.getcwd()
    root = csearch.repo_root(Path(fpath).parent if Path(fpath).parent.exists()
                             else Path(cwd))
    # Outside a git repo the fallback root is the file's own folder, and for
    # ~/Documents/x.py that means indexing all of ~/Documents. Not worth it.
    if not (root / ".git").exists():
        return 0
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
            same = [x for x in idx.get(nf["fp"], []) if x[0] != rel_self]
            if not same or nf["fp"] in warned:
                continue
            fresh_fp = nf["fp"]
            where = "\n".join(f"  - `{r}:{l}` → `{n}`" for r, n, l, _ in same[:3])
            clone_blocks.append(
                f"**`{nf['n']}`** — this exact body ({nf['t']} tokens) already "
                f"exists under another name:\n{where}")
            warned = warned | {fresh_fp}
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

    # Repo conventions: once per session, on the first code write.
    if not st.get("conv"):
        conv = CONV_DIR / f"{root.name}.md"
        if conv.exists():
            try:
                body = conv.read_text().strip()
                if body:
                    parts.append(
                        f"## Conventions for {root.name}\n\n{body}"
                    )
            except Exception:
                pass
        st["conv"] = True

    if clone_blocks:
        parts.append(
            "## Reuse: duplicated body\n\n"
            + "\n\n".join(clone_blocks)
            + "\n\n_This is not a similar name: it is the same code. Extract "
            "one function and parametrise the difference._"
        )

    if blocks:
        parts.append(
            "## Reuse: possible duplication\n\n"
            + "\n\n".join(blocks)
            + "\n\n_Before writing: read the candidates above. If one fits, "
            "import or extend it instead of rewriting. If none fits, carry on — "
            "but say in one line why not._"
        )

    if parts:
        out("\n\n".join(parts))
        st["warned"] = sorted(warned | set(fresh))
        save_state(sid, st)

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
