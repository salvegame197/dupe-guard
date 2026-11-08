#!/usr/bin/env python3
"""
Hook SessionEnd: write the session's trace to the vault, without anyone having
to remember.

The vault is the only durable store. Local transcripts are deleted after a few
weeks; what never becomes a note is lost, not by design but by retention. When
writing it down depends on the model remembering at the end of a session, days
of work go unrecorded. This hook closes that leak.

Two levels, deliberately separate (see distill.py for the second):

  1. CHEAP and deterministic — this file. Parsing and regex over the
     transcript, no model call, ~100ms. Always runs. Records what is
     verifiable: project, branch, requests, files, commits, tokens.
  2. EXPENSIVE — the distiller that rewrites understanding (decisions,
     pitfalls) into the project note. Needs an LLM. Here it is only QUEUED:
     SessionEnd runs while the process is shutting down and cannot block, so
     a `claude -p` fired from here risks being killed halfway.

Silence is the default: a session without substance writes nothing, and no
failure here may reach the screen or break the user's exit.
"""

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, fold, is_dataless, labels, load_config, redact   # noqa: E402

QUEUE = DATA / "distill-queue.jsonl"
ERRORS = DATA / "session-log-errors.log"

MIN_PROMPT_CHARS = 25   # same threshold as recall: below it is an acknowledgement
MIN_PROMPTS = 3         # fewer than this with no file touched is not a work session
MAX_PROMPTS_SHOWN = 6
MAX_FILES_SHOWN = 12

CODE_EXT = (
    "php|ts|tsx|js|jsx|mjs|cjs|py|rs|go|java|kt|swift|rb|sql|md|json|yaml|yml|"
    "toml|vue|svelte|css|scss|html|sh|prisma|proto"
)
PATH_RE = re.compile(rf"[\w][\w./@+-]*\.(?:{CODE_EXT})\b")

# Never "work on the project": the tool's own meta files, dependencies, build.
NOISE = re.compile(r"(^|/)(node_modules|\.git|dist|build|vendor|\.next|target)/|(^|/)\.claude/")

# Where a rule the model learned goes without the brain: Claude Code's own
# memory (one note per file, under the session's project folder) and a
# CLAUDE.md. A session that wrote one of these has already recorded itself.
CLAUDE_MD = re.compile(r"(^|/)CLAUDE\.md$")
MEMORY_NOTE = re.compile(r"/memory/[^/]+\.md$")


def is_learned(path: str, transcripts=None) -> bool:
    """A CLAUDE.md anywhere, or a memory note under the projects folder
    (the configured one, or the default ~/.claude/projects)."""
    if not path:
        return False
    if CLAUDE_MD.search(path):
        return True
    if not MEMORY_NOTE.search(path):
        return False
    return "/.claude/projects/" in path or (transcripts is not None and path.startswith(str(transcripts) + "/"))


def note_error(msg: str) -> None:
    """A line in the error log the next SessionStart reports. Silent on failure."""
    try:
        ERRORS.parent.mkdir(parents=True, exist_ok=True)
        with ERRORS.open("a", encoding="utf-8") as fh:
            fh.write(f"\n=== {datetime.now().isoformat()} ===\n{msg}\n")
    except OSError:
        pass


def note_for(cwd: str, cfg: dict):
    """(note name, orphan?). An orphan is not an error: work with no page yet."""
    if not cwd:
        return None, True
    base = Path(cwd).name
    if base in {"", Path.home().name, *cfg["no_project_dirs"]}:
        return None, False                # home or scratch: no project
    alias = {fold(k): v for k, v in cfg["project_aliases"].items()}.get(fold(base))
    if alias:
        return alias, False
    # Exact match by .md file name anywhere in the vault. An empty file does
    # not count: editors leave empty notes behind with a real note's name.
    for p in cfg["vault"].rglob(f"{base}.md"):
        try:
            if ".obsidian" not in p.parts and p.stat().st_size > 0:
                return p.stem, False
        except OSError:
            continue
    return base, True


def parse(path: Path) -> dict:
    """Extract only what is verifiable from the transcript: no interpretation."""
    out = {
        "cwd": None, "branch": None, "title": None,
        "prompts": [], "files": set(), "written": set(), "first_ts": None, "last_ts": None,
        "turns": 0,
        # Tokens per session. Transcripts expire and usage dashboards only
        # look back so far, so this is the one place the number survives.
        "tokens": {"in": 0, "out": 0, "cache_w": 0, "cache_r": 0},
    }
    try:
        fh = path.open(encoding="utf-8", errors="ignore")
    except OSError:
        return out

    with fh:
        for line in fh:
            try:
                d = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if d.get("cwd"):
                out["cwd"] = d["cwd"]
            if d.get("gitBranch"):
                out["branch"] = d["gitBranch"]
            if d.get("type") == "ai-title" and d.get("aiTitle"):
                out["title"] = d["aiTitle"]
            ts = d.get("timestamp")
            if ts:
                out["first_ts"] = out["first_ts"] or ts
                out["last_ts"] = ts

            t = d.get("type")
            if t == "user" and not d.get("isMeta"):
                c = (d.get("message") or {}).get("content")
                if isinstance(c, str):
                    s = c.strip()
                    # '<' = injected system reminder; '/' = slash command
                    if s and not s.startswith("<") and not s.startswith("/"):
                        out["prompts"].append(s)
            elif t == "assistant":
                out["turns"] += 1
                u = (d.get("message") or {}).get("usage") or {}
                if isinstance(u, dict):
                    out["tokens"]["in"] += u.get("input_tokens") or 0
                    out["tokens"]["out"] += u.get("output_tokens") or 0
                    out["tokens"]["cache_w"] += u.get("cache_creation_input_tokens") or 0
                    out["tokens"]["cache_r"] += u.get("cache_read_input_tokens") or 0
                for b in (d.get("message") or {}).get("content") or []:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    name, inp = b.get("name"), b.get("input") or {}
                    if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
                        if inp.get("file_path"):
                            out["files"].add(inp["file_path"])
                            out["written"].add(inp["file_path"])
                    elif name == "Bash":
                        # Edits often happen through sed or heredocs inside
                        # Bash, where no Edit tool_use ever appears. Without
                        # scanning the command the whole session is invisible.
                        for m in PATH_RE.findall(inp.get("command") or ""):
                            out["files"].add(m)
    return out


def learned_files(written, transcripts=None) -> list:
    """The memory notes and CLAUDE.md files the model wrote through Edit or
    Write (not Bash: a `cat CLAUDE.md` is not learning). Idempotent and
    cheap; the paths are what the transcript says, not checked on disk."""
    return sorted({f for f in written if is_learned(f, transcripts)})


def resolve_files(files, cwd: str):
    """
    Keep only paths that EXIST and are INSIDE the session's repo.

    The regex over Bash commands invents paths that never existed (a file name
    from an example), and what exists outside the repo is almost always
    scratch or /tmp, which also collides on relative names.
    """
    if not cwd:
        return []
    base = Path(cwd)
    keep = {}
    for f in sorted(files):
        if NOISE.search(f):
            continue
        p = Path(f)
        if not p.is_absolute():
            p = base / f
        try:
            if not p.exists():
                continue
            rel = p.resolve().relative_to(base.resolve())
        except (OSError, ValueError):
            continue        # outside the repo: not work on the project
        keep[str(rel)] = True
    return sorted(keep)[:MAX_FILES_SHOWN]


def commits(cwd: str, since: str):
    """
    The USER's commits within the session window.

    Without the author filter the time window picks up other people's merged
    PRs and credits the session with work that was not its own. Recording too
    little beats lying.
    """
    if not cwd or not since:
        return []
    try:
        who = subprocess.run(["git", "-C", cwd, "config", "user.email"],
                             capture_output=True, text=True, timeout=5).stdout.strip()
        cmd = ["git", "-C", cwd, "log", "--since", since, "--no-merges", "--pretty=%h %s"]
        if who:
            cmd[4:4] = ["--author", who]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return []
        return [l for l in r.stdout.splitlines() if l.strip()][:8]
    except (subprocess.SubprocessError, OSError):
        return []


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    cfg = load_config()
    if not cfg["enabled"]:
        return 0

    tp = payload.get("transcript_path") or ""
    sid = payload.get("session_id") or ""
    reason = payload.get("reason") or "other"
    if not tp or not Path(tp).exists():
        return 0

    info = parse(Path(tp))
    cwd = info["cwd"] or payload.get("cwd") or ""
    real = [p for p in info["prompts"] if len(p) >= MIN_PROMPT_CHARS]
    files = resolve_files(info["files"], cwd)
    learned = learned_files(info["written"], cfg["transcripts"])

    # A correction the user made in this session becomes a draft line in the
    # repo's CLAUDE.md or the native memory (brain/learned.py). Before the
    # substance filter: one "no, it is X" with an edit is substance even in
    # a two-prompt session. Never lets a failure reach the exit.
    taught = {}
    try:
        from brain import learned as learned_mod
        taught = learned_mod.run(tp, sid, cwd, cfg)
    except Exception:
        taught = {}

    # Substance filter: a one-question session, or only acknowledgements, does
    # not deserve a line in the vault. Polluting is worse than not recording.
    if len(real) < MIN_PROMPTS and not (real and files):
        return 0

    when = datetime.now()
    try:
        when = datetime.fromisoformat((info["last_ts"] or "").replace("Z", "+00:00")).astimezone()
    except ValueError:
        pass
    day = when.strftime("%Y-%m-%d")
    note, orphan = note_for(cwd, cfg)
    L = labels(cfg)

    # Idempotency: SessionEnd can fire more than once for the same id (clear,
    # then resume). The marker carries the turn count, so a session that GREW
    # after a clear gets a new block and a repeated one does not.
    mark = f"<!-- sid:{sid} turns:{info['turns']} -->"
    raw_dir = cfg["vault"] / cfg["raw_dir"]
    raw_dir.mkdir(parents=True, exist_ok=True)
    target = raw_dir / f"{day}.md"
    # The vault may sit in a cloud drive that evicts files from disk; reading
    # one then blocks until it is downloaded, and SessionEnd hung on exactly
    # this line (TimeoutError in session-log-errors.log). Evicted: do not
    # touch the page, note it in the error log (the next SessionStart shows
    # it) and still queue the session, which the distiller can reach without
    # the raw block.
    evicted = is_dataless(target)
    if evicted:
        note_error(f"{target} is not on disk (cloud placeholder); session {sid} was queued "
                   f"but not written to the vault. On macOS: brctl download \"{target}\"")
    elif target.exists() and mark in target.read_text(encoding="utf-8", errors="ignore"):
        return 0

    lines = [mark, ""]
    head = f"## {when.strftime('%H:%M')}"
    if note:
        head += f" — {note}" + (f" {L['no_note']}" if orphan else "")
    if info["branch"] and info["branch"] not in ("main", "master"):
        head += f" · `{info['branch']}`"
    lines += [head, ""]
    if info["title"]:
        lines.append(f"**{L['subject']}:** {info['title']}")
    if note and not orphan:
        lines.append(f"**{L['note']}:** [[{note}]]")
    if cwd:
        lines.append(f"**{L['repo']}:** `{cwd}`")
    lines += [f"**{L['ended_by']}:** {reason}", ""]

    # Requests go through redact(): a key pasted into a prompt must not end
    # up in the vault (synced to a cloud drive) nor in the queue.
    shown = [redact(" ".join(p.split()))[:200] for p in real[:MAX_PROMPTS_SHOWN]]
    lines.append(f"**{L['requests']}:**")
    for one, p in zip(shown, real):
        lines.append(f"- {one}{'…' if len(' '.join(p.split())) > 200 else ''}")
    if len(real) > MAX_PROMPTS_SHOWN:
        lines.append(f"- _(+{len(real) - MAX_PROMPTS_SHOWN} {L['more_requests']})_")
    lines.append("")

    if files:
        lines += [f"**{L['files_touched']}:** " + ", ".join(f"`{f}`" for f in files), ""]
    if learned:
        shown_learned = ", ".join(f"`{Path(f).name}`" for f in learned[:4])
        lines += [f"_{L['distill_skipped'].format(files=shown_learned)}_", ""]
    if taught.get("written") or taught.get("pending"):
        where = {l["path"] for l in taught.get("lines", []) if l["status"] in ("written", "pending")}
        lines += [f"**{L['learned_written']}:** {taught.get('written', 0)}"
                  + (f" (+{taught['pending']} {L['learned_pending']})" if taught.get("pending") else "")
                  + " → " + ", ".join(f"`{Path(p).name}`" for p in sorted(where)), ""]
    cs = commits(cwd, info["first_ts"] or "")
    if cs:
        lines.append(f"**{L['commits']}:**")
        lines.extend(f"- `{c}`" for c in cs)
        lines.append("")
    tk = info["tokens"]
    if any(tk.values()):
        # Fixed fields in a fixed order, so the whole history sums with
        #   grep -h '^_Tokens:' Sessions/Raw/*.md
        lines += [f"_Tokens: in={tk['in']} out={tk['out']} "
                  f"cache_w={tk['cache_w']} cache_r={tk['cache_r']}_", ""]

    # The harness proposes changes to itself from this session's evidence
    # (recall injecting a page nobody read, a correction the model admitted).
    # It only proposes: the decision belongs to the user.
    try:
        from brain import proposals
        props, pinfo = proposals.generate(tp, sid, cwd, cfg)
        n_props = proposals.record(props, pinfo, sid, cfg, when)
        if n_props:
            page = Path(cfg["proposals_page"]).stem
            lines += [f"**{L['proposals']}:** {n_props} — {L['see']} [[{page}]]", ""]
    except Exception:
        pass

    # Rules prove themselves or retire: one session's evidence updates the
    # counters of the conventions it injected (see brain/conventions.py).
    try:
        from brain import conventions
        t = conventions.tally(tp, cfg)
        if t["up"] or t["down"]:
            lines += [f"_{L['conventions']}: +{t['up']} −{t['down']}"
                      + (f", {t['retired']} {L['retired']}" if t["retired"] else "") + "_", ""]
    except Exception:
        pass

    # The transcript path is deliberate: while it lives, the detail can be
    # reopened. After that, what remains is what is written here.
    lines += [f"_Transcript: `{tp}` — {L['transcript_expires']}._", "", "---", ""]

    block = "\n".join(lines)
    header = f"---\ntags: {L['raw_tags']}\n---\n\n# {L['sessions_of']} {day}\n\n"
    if not evicted:
        try:
            new = not target.exists()
            # Append-only is the only safe mode: the vault may sync through a
            # cloud drive and another session may be ending at the same moment.
            with target.open("a", encoding="utf-8") as fh:
                if new:
                    fh.write(header)
                fh.write(block)
        except OSError:
            return 0

    # Queue for the expensive distiller. The queue is the contract between the
    # two levels. A session with no note (home, no project) stays in the raw
    # log only: there is nowhere to distill it to.
    # A session in which the model already wrote a native memory note or a
    # CLAUDE.md has recorded its own lesson where Claude Code reads it; the
    # distiller would write the same thing again into the project note. The
    # row still goes to the queue file, marked `skipped` with the reason, so
    # the decision is on record (distill.queue ignores it).
    if not note:
        return 0
    try:
        QUEUE.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": when.isoformat(), "session_id": sid, "day": day,
            "note": note, "orphan": orphan, "cwd": cwd,
            "transcript": tp, "raw": str(target),
            # the same 200 redacted chars the raw page shows, never the
            # whole prompt: the distiller reads the transcript itself
            "files": files, "prompts": shown,
        }
        if learned:
            row["skipped"] = "wrote " + ", ".join(learned[:6])
        with QUEUE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A failure here must not block the exit, but it must not pass silently
        # either: the whole system rests on every session being recorded. The
        # error is written down and the next SessionStart reports it.
        try:
            import traceback
            ERRORS.parent.mkdir(parents=True, exist_ok=True)
            with ERRORS.open("a", encoding="utf-8") as fh:
                fh.write(f"\n=== {datetime.now().isoformat()} ===\n")
                traceback.print_exc(file=fh)
            print(f"dupe-guard session log failed — see {ERRORS}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(0)
