#!/usr/bin/env python3
"""
learned — a correction the user made becomes one draft line where the next
session will read it.

The richest signal a transcript carries is the user saying "no, it is X": a
rule in the user's own words. Until 2026-11-08 it became a `convention`
proposal on a page nobody decided on (21 pending in seven weeks, none
applied). Now the TRIAD below becomes one line, at the end of the session,
with no model call:

  1. a HUMAN prompt corrects or states a rule (the correction phrases of
     brain/proposals.py plus rule words: "nunca", "sempre", "não vamos",
     "tem que ser", "já te disse"); questions and bug reports ("deu errado")
     are not corrections;
  2. the model's reply in the same turn admits or accepts it ("tem razão",
     "corrijo", "vou tirar", or an opening "Combinado"/"Entendido");
  3. the model edited something in that turn (Edit/Write, not in /tmp): the
     correction had an effect.

Where the line goes (the three stores of docs/IDEIAS §4):
  - the session's repo is the user's own (no remote, or a remote under
    `learned.own_remotes`): `CLAUDE.md` of the repo, section
    `## Aprendido (revisar)`, newest line first. Approving is the commit the
    user already makes; rejecting is deleting the line.
  - otherwise, or outside any repo: the native memory of that working
    directory, `~/.claude/projects/<folder>/memory/aprendido-<slug>.md`
    (type feedback) with a pointer in MEMORY.md.

Guards: at most 3 lines per session; never the same line twice (compared
folded); never touch a CLAUDE.md that has an uncommitted change made by
someone else (the line waits in aprendido-pendente.jsonl and is retried at
the next session end); a turn whose only edit was a memory note or a
CLAUDE.md already recorded itself and is skipped. Every decision goes to
aprendido-log.jsonl, which hit_rate.py reads to count what was written,
what survived a commit and what was deleted.

Usage:
  learned.py --dry-run [transcript.jsonl ...]   # every transcript; writes nothing
  learned.py --pending                           # what is waiting for a clean CLAUDE.md
"""
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import (DATA, DEFAULTS, WORKER_DIR, fold, is_dataless, labels,      # noqa: E402
                        load_config, project_folder_name, redact, settings)

LOG = DATA / "aprendido-log.jsonl"
PENDING = DATA / "aprendido-pendente.jsonl"
PENDING_DAYS = 30            # a line nobody could place in a month is dropped
MAX_PROMPT_CHARS = 600       # longer than this is a task brief, not a correction
MAX_LINE_CHARS = 240
EDITS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SCRATCH = ("/tmp/", "/private/tmp/")      # the model's scratchpad lives under /tmp/claude-*

# A "no" that opens a question or a status report, not a correction.
NOT_CORRECTION = {
    "pt": (r"^\s*n[aã]o\s+(entendi|entnedi|sei|tem problema|precisa|precisamos|seria|parece|"
           r"apareceu|encontrei|achei|consigo|consegui|funcionou|funciona|deu|abriu|veio|rolou|"
           r"carregou|aparece|mostra|ta\b|t[aá]\b|est[aá]\b|esta\b|foi poss[ií]vel|valem|zerou|lembro|"
           r"importa|faz mal|vejo|to vendo|estou vendo|tenho|tinha|tem\b|existe\?)"),
    "en": r"^\s*no\s+(idea|problem|worries|need|clue|luck|change|output|error)\b|^\s*not (sure|yet|working)\b",
}
# "it went wrong" is a bug report, not the user telling the model it is wrong.
BUG_REPORT = {
    "pt": r"\b(d(eu|ando|ar|a|á)|est[aá]|t[aá]|fica|ficou|foi|vai|sai|saiu) errad[oa]\b|\berrado\s*:",
    "en": r"\b(went|goes|going|is|was|got|comes out) wrong\b|\bwrong\s*:",
}
# The user stating a rule, with or without a leading "no".
RULE = {
    "pt": (r"\b(nunca|jamais|sempre|n[aã]o (pode|podemos|vamos|deve|devemos|existe|quero|queremos|"
           r"use|usa|usar|faz|fa[cç]a|fazer|cria|crie|criar|mexe|mexa|deleta|delete|deletar|apaga|"
           r"apague|apagar|roda|rode|rodar|é assim|e assim|era isso|é isso|foi isso|vou usar)|"
           r"tem que ser|tem que (?!ver\b)|temos que (?!ver\b)|j[aá] (te )?(disse|falei|pedi|expliquei)|"
           r"eu (disse|pedi|falei|expliquei)|n[aã]o é problema)\b"),
    "en": (r"\b(never|always|do not|don't|must not|mustn't|should not|shouldn't|we (don't|do not|never|won't)|"
           r"has to be|must be|i (told|said|asked) you|as i said|not our problem)\b"),
}
# Strong enough that a question mark in the prompt does not disqualify it.
STRONG = {
    "pt": (r"\b(nunca|jamais|sempre|n[aã]o (pode|podemos|vamos|deve|devemos|existe)|tem que ser|"
           r"j[aá] (te )?(disse|falei|pedi|expliquei))\b"),
    "en": r"\b(never|always|must not|mustn't|has to be|must be|i (told|said|asked) you)\b",
}
# The model giving in, anywhere in its turn (on top of proposals.ADMISSION).
ADMISSION_MORE = {
    "pt": (r"corre[cç][aã]o:|corrijo|vou corrigir|passei por cima|est[aá] errad[ao]|estava errad[ao]|"
           r"me confundi|eu assumi|assumi (errado|que)|desnecess[aá]ri[ao]|vou tirar|vou desfazer|"
           r"vou reverter|n[aã]o resolve|entendi errado|interpretei"),
    "en": r"correction:|i assumed|i missed|i'll fix|i will fix|that was unnecessary|i'll remove|i'll revert|i misread",
}
# ... or accepting it in the first words of the reply.
ACCEPT_OPENING = {
    "pt": (r"^\W*(combinado|entendido|entendi|agora entendi|certo|fechado|concordo|desculpa|desculpe|"
           r"boa cobran[cç]a|seu ponto|verdade|faz sentido|beleza|sim[,.]|(voc[eê] )?tem raz[aã]o)"),
    "en": r"^\W*(got it|understood|agreed|you're right|you are right|fair|right,|yes,|sorry|my bad|noted)",
}
WHY = {"pt": r"\b(porque|pois|por isso|sen[aã]o|j[aá] que|because)\b", "en": r"\b(because|since|otherwise|so that)\b"}
NEGATIVE = {"pt": r"\b(nunca|jamais|n[aã]o|sem|never|not|don't)\b", "en": r"\b(never|not|don't|do not|no|without)\b"}
CONNECTOR = {"pt": r"^(ent[aã]o|e |mas|por isso|da[ií]|a[ií]|logo|portanto)\b", "en": r"^(so|and|but|then|hence|therefore)\b"}
OPENERS = {"pt": r"^(cara|pera|olha|ok|ent[aã]o|tipo|mas|n[aã]o n[aã]o|bom|ent[aã]o|a[ií])\b[,\s]+",
           "en": r"^(ok|okay|well|so|look|hey|no no|but)\b[,\s]+"}

MARK = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · (?:sess[aã]o|session) ([\w-]{1,8}) · ")


def patterns(cfg):
    from brain.proposals import ADMISSION, CORRECTION
    langs = cfg["languages"]

    def rx(table, flags=re.I):
        return re.compile("|".join(table[l] for l in langs if l in table), flags)

    return {
        "correction": rx(CORRECTION),
        "not_correction": rx(NOT_CORRECTION),
        "bug": rx(BUG_REPORT),
        "rule": rx(RULE),
        "strong": rx(STRONG),
        "admission": re.compile("|".join([ADMISSION[l] for l in langs if l in ADMISSION]
                                         + [ADMISSION_MORE[l] for l in langs if l in ADMISSION_MORE]), re.I),
        "accept": rx(ACCEPT_OPENING),
        "why": rx(WHY),
        "negative": rx(NEGATIVE),
        "opener": rx(OPENERS),
        "connector": rx(CONNECTOR),
    }


# ─── detection ───────────────────────────────────────────────────────────

def is_correction(text: str, P) -> bool:
    """The prompt corrects the model or states a rule. A question without a
    strong rule word is a question; "deu errado" is a bug report."""
    t = (text or "").strip()
    if not t or len(t) > MAX_PROMPT_CHARS:
        return False
    if "?" in t and not P["strong"].search(t):
        return False
    if P["rule"].search(t):
        return True
    return bool(P["correction"].search(t)) and not P["not_correction"].match(t) and not P["bug"].search(t)


def admitted(reply: str, P):
    """The admission phrase in the model's turn, or None."""
    reply = reply or ""
    m = P["admission"].search(reply) or P["accept"].match(reply.strip()[:120])
    return m.group(0) if m else None


def triads(path, cfg, P=None):
    """[{prompt_idx, admit_idx, edit_idx, prompt, admission, edited,
    self_recorded}] for one transcript. Indices are positions in
    hit_rate.events(); `self_recorded` when every edit of the turn was a
    memory note or a CLAUDE.md (the model already wrote the lesson down)."""
    from brain import hit_rate, ksearch
    from brain.session_log import is_learned
    P = P or patterns(cfg)
    ev = hit_rate.events(path)
    first = next((d["text"] for _, k, d in ev if k == "prompt"), "")
    if " ".join(first[:60].split()).startswith(ksearch.WORKER_PROMPTS):
        return []                                  # the brain's own child
    out = []
    for i, (ts, kind, d) in enumerate(ev):
        if kind != "prompt" or not d.get("human") or not is_correction(d["text"], P):
            continue
        texts, edits, admit_at = [], [], None
        for j in range(i + 1, len(ev)):
            _, k2, d2 = ev[j]
            if k2 == "prompt":
                break
            if k2 == "text":
                texts.append(d2["text"])
                if admit_at is None and admitted(" ".join(texts), P):
                    admit_at = j
            elif k2 == "tool" and d2["name"] in EDITS and d2["path"] and not d2["path"].startswith(SCRATCH):
                edits.append((j, d2["path"]))
        adm = admitted(" ".join(texts), P)
        if not adm or not edits:
            continue
        out.append({
            "prompt_idx": i, "admit_idx": admit_at, "edit_idx": edits[0][0],
            "prompt": d["text"], "admission": adm, "edited": [p for _, p in edits][:4],
            "self_recorded": all(is_learned(p, cfg.get("transcripts")) for _, p in edits),
            "ts": ts,
        })
    return out


# ─── the line ────────────────────────────────────────────────────────────

def rule_text(prompt: str, P) -> str:
    """The user's sentence that carries the rule, with its neighbours when
    they are short or say why. The words stay the user's: this is a draft,
    and a paraphrase without a model would only lose the reason."""
    t = redact(" ".join((prompt or "").split()))
    t = re.sub(r"\b(k{2,}|haha+|rs{2,})\b", "", t, flags=re.I)
    parts = [s.strip(" .?!") for s in re.split(r"(?<=[.!?])\s+|\.{2,}\s*", t) if s.strip(" .?!")]
    if not parts:
        return ""
    idx = next((n for n, s in enumerate(parts)
                if P["strong"].search(s) or P["rule"].search(s) or P["correction"].search(s)), 0)
    chosen = [parts[idx]]
    # a sentence that opens with a connector ("entao nao vamos...", "e
    # estourar esse limite...") needs the one before it to make sense
    if idx > 0 and len(parts[idx - 1]) <= 100 and (len(parts[idx]) < 40 or P["connector"].match(parts[idx])):
        chosen.insert(0, parts[idx - 1])
    if idx + 1 < len(parts) and (P["why"].search(parts[idx + 1]) or len(parts[idx]) < 40):
        chosen.append(parts[idx + 1])
    out = ". ".join(chosen)
    for _ in range(2):                       # "cara mas eu..." has two openers
        out = P["opener"].sub("", out).strip(" ,;:-")
    out = " ".join(out.split())
    if len(out) > MAX_LINE_CHARS:
        out = out[:MAX_LINE_CHARS].rsplit(" ", 1)[0] + "…"
    return out[:1].upper() + out[1:] if out else out


def polarity(text: str, P, L) -> str:
    return L["learned_dont"] if P["negative"].search(text or "") else L["learned_do"]


def line_for(triad: dict, day: str, sid: str, cfg, P=None) -> str:
    P = P or patterns(cfg)
    L = labels(cfg)
    text = rule_text(triad["prompt"], P)
    if not text:
        return ""
    return f'- {day} · {L["learned_session"]} {(sid or "nosession")[:8]} · {polarity(text, P, L)}: "{text}"'


def normalized(line: str) -> str:
    """The rule text of a line, folded to letters and digits, for the
    duplicate check. A hand-written line without our prefix is compared
    whole."""
    m = re.search(r'"(.*)"\s*$', line)
    body = m.group(1) if m else MARK.sub("", line)
    return re.sub(r"[^a-z0-9]+", "", fold(body))


def is_duplicate(line: str, existing: str) -> bool:
    key = normalized(line)
    if not key:
        return True
    for other in existing.splitlines():
        o = normalized(other)
        if o and (o == key or (len(key) >= 30 and key in o) or (len(o) >= 30 and o in key)):
            return True
    return False


# ─── destination ─────────────────────────────────────────────────────────

def git_root(cwd: str) -> str:
    if not cwd or not Path(cwd).is_dir():
        return ""
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def remote_url(root: str) -> str:
    try:
        r = subprocess.run(["git", "-C", root, "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def plain_remote(url: str) -> str:
    """`git@github.com:me/x.git`, `https://github.com/me/x.git`,
    `ssh://git@github.com/me/x` all become `github.com/me/x`."""
    u = (url or "").strip()
    u = re.sub(r"^[a-z+]+://", "", u)
    u = re.sub(r"^[^@/]+@", "", u)
    u = u.replace(":", "/", 1) if re.match(r"^[^/]+:[^/]", u) else u
    u = re.sub(r"\.git$", "", u)
    return u.lower().rstrip("/")


def owned(root: str, own_remotes, remote: str = None) -> bool:
    """The repo is the user's: no remote, or the remote is under one of the
    configured prefixes (`github.com/me/`). Anything else is someone else's
    repo, where a CLAUDE.md line would be a change in their tree."""
    remote = remote_url(root) if remote is None else remote
    if not remote:
        return True
    plain = plain_remote(remote)
    for p in (own_remotes or ()):
        pre = plain_remote(p)
        if pre and (plain == pre or plain.startswith(pre + "/")):
            return True
    return False


def slug(cwd: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold(Path(cwd).name)).strip("-")
    return s or "home"


def destination(cwd: str, cfg, root: str = None, remote: str = None):
    """("claude_md", path, repo) | ("memory", path, repo) | (None, None, repo)."""
    if not cwd:
        return None, None, ""
    conf = settings(cfg, "learned", DEFAULTS["learned"])
    root = git_root(cwd) if root is None else root
    repo = Path(root).name if root else Path(cwd).name
    if root and owned(root, conf.get("own_remotes"), remote):
        return "claude_md", Path(root) / "CLAUDE.md", repo
    folder = Path(cfg["transcripts"]) / project_folder_name(cwd) / "memory"
    return "memory", folder / f"aprendido-{slug(cwd)}.md", repo


# ─── the CLAUDE.md guard ─────────────────────────────────────────────────

def ours(line: str, L) -> bool:
    s = line.strip()
    return not s or s == f"## {L['learned_section']}" or bool(MARK.match(s))


def foreign_changes(root: str, L) -> bool:
    """True when CLAUDE.md has an uncommitted change that is not ours: a
    deletion, or an added line that is neither the section heading nor a
    line with our stamp. An untracked file counts unless every line is
    ours. Writing over somebody's edit in progress is the one thing this
    must never do."""
    path = Path(root) / "CLAUDE.md"
    try:
        st = subprocess.run(["git", "-C", root, "status", "--porcelain", "--", "CLAUDE.md"],
                            capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return True
    if not st:
        return False
    if st.startswith("??"):
        try:
            return not all(ours(l, L) for l in path.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            return True
    for extra in ((), ("--cached",)):
        try:
            diff = subprocess.run(["git", "-C", root, "diff", *extra, "--", "CLAUDE.md"],
                                  capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return True
        for l in diff.splitlines():
            if l.startswith("---") or l.startswith("+++"):
                continue
            if l.startswith("-") and l[1:].strip():
                return True
            if l.startswith("+") and not ours(l[1:], L):
                return True
    return False


# ─── writing ─────────────────────────────────────────────────────────────

def insert_in_section(txt: str, heading: str, line: str) -> str:
    """The line right under the heading (newest first), contiguous with the
    bullets already there; the section at the end of the file when it does
    not exist."""
    m = re.search(rf"^{re.escape(heading)}[ \t]*$", txt, re.M)
    if not m:
        base = txt.rstrip("\n")
        return (base + "\n\n" if base else "") + f"{heading}\n\n{line}\n"
    rest = txt[m.end():].lstrip("\n")
    if rest and not rest.startswith("- "):
        rest = "\n" + rest
    return txt[:m.end()] + "\n\n" + line + "\n" + rest


def write_claude_md(path: Path, line: str, L) -> str:
    """written | duplicate."""
    txt = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    if is_duplicate(line, txt):
        return "duplicate"
    path.write_text(insert_in_section(txt, f"## {L['learned_section']}", line), encoding="utf-8")
    return "written"


def write_memory(path: Path, line: str, repo: str, L, when: datetime) -> str:
    """written | duplicate. Creates the note (type feedback) and the
    pointer in MEMORY.md on first use."""
    txt = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
    if txt and is_duplicate(line, txt):
        return "duplicate"
    stamp = when.strftime("%Y-%m-%dT%H:%M:%S")
    if not txt:
        txt = (f"---\nname: {path.stem}\n"
               f"description: {L['learned_memory_desc'].format(repo=repo)}\n"
               f"metadata:\n  node_type: memory\n  type: feedback\n  modified: {stamp}\n---\n\n"
               f"# {L['learned_section']} — {repo}\n\n{line}\n")
    else:
        txt = re.sub(r"^(\s*modified:) .*$", rf"\1 {stamp}", txt, count=1, flags=re.M)
        txt = insert_in_section(txt, f"# {L['learned_section']} — {repo}", line)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(txt, encoding="utf-8")
    index = path.parent / "MEMORY.md"
    pointer = f"- [{L['learned_pointer'].format(repo=repo)}]({path.name}) — {L['learned_memory_desc'].format(repo=repo)}"
    try:
        idx = index.read_text(encoding="utf-8", errors="ignore") if index.exists() else "# Memory Index\n\n"
        if f"]({path.name})" not in idx:
            index.write_text(idx.rstrip("\n") + "\n" + pointer + "\n", encoding="utf-8")
    except OSError:
        pass
    return "written"


def place(entry: dict, cfg, L, when: datetime, dry: bool = False) -> str:
    """Write one entry where it belongs. Returns the status."""
    kind, path, _ = entry["kind"], Path(entry["path"]), entry.get("repo")
    if dry:
        return "dry-run"
    if is_dataless(path):
        return "pending"
    if kind == "claude_md":
        if foreign_changes(str(path.parent), L):
            return "pending"
        return write_claude_md(path, entry["line"], L)
    return write_memory(path, entry["line"], entry.get("repo") or "", L, when)


def log(entry: dict) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_jsonl(path: Path) -> list:
    out = []
    try:
        for l in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                out.append(json.loads(l))
            except ValueError:
                continue
    except OSError:
        pass
    return out


def write_jsonl(path: Path, rows) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    except OSError:
        pass


def retry_pending(cfg, L, when: datetime) -> int:
    """Lines that waited for a clean CLAUDE.md: placed now if they can be,
    dropped after PENDING_DAYS, kept otherwise."""
    rows = read_jsonl(PENDING)
    if not rows:
        return 0
    keep, placed = [], 0
    for r in rows:
        age = (time.time() - (r.get("ts") or time.time())) / 86400
        if age > PENDING_DAYS:
            log({**r, "status": "expired", "at": when.isoformat()})
            continue
        status = place(r, cfg, L, when)
        if status == "pending":
            keep.append(r)
            continue
        log({**r, "status": status, "at": when.isoformat(), "retried": True})
        placed += status == "written"
    write_jsonl(PENDING, keep)
    return placed


def run(transcript: str, sid: str, cwd: str, cfg, when: datetime = None, dry: bool = False) -> dict:
    """The whole thing for one session: triads -> lines -> destination ->
    write or queue, every decision logged. Returns counts."""
    conf = settings(cfg, "learned", DEFAULTS["learned"])
    when = when or datetime.now()
    out = {"triads": 0, "written": 0, "pending": 0, "duplicate": 0, "skipped": 0, "lines": []}
    if not conf.get("enabled") and not dry:
        return out
    L = labels(cfg)
    P = patterns(cfg)
    if not dry:
        retry_pending(cfg, L, when)
    found = triads(transcript, cfg, P)
    out["triads"] = len(found)
    if not found:
        return out
    day = when.strftime("%Y-%m-%d")
    kind, path, repo = destination(cwd, cfg)
    cap = int(conf.get("max_per_session") or 3)
    placed = 0
    for t in found:
        line = line_for(t, day, sid, cfg, P)
        base = {"ts": int(time.time()), "at": when.isoformat(), "session": sid, "cwd": cwd, "repo": repo,
                "kind": kind, "path": str(path) if path else None, "line": line,
                "triad": {"prompt": t["prompt_idx"], "admit": t["admit_idx"], "edit": t["edit_idx"],
                          "admission": t["admission"], "edited": t["edited"]}}
        if not line:
            status = "no-text"
        elif t["self_recorded"]:
            status = "self-recorded"
        elif not kind:
            status = "no-destination"
        elif placed >= cap:
            status = "capped"
        else:
            status = place(base, cfg, L, when, dry)
            placed += status in ("written", "pending", "dry-run")
        if status == "pending" and not dry:
            write_jsonl(PENDING, read_jsonl(PENDING) + [base])
        if not dry:
            log({**base, "status": status})
        out[status if status in out else "skipped"] = out.get(status if status in out else "skipped", 0) + 1
        out["lines"].append({"status": status, "line": line, "kind": kind, "path": str(path) if path else None,
                             "repo": repo, "prompt": t["prompt"][:200], "admission": t["admission"],
                             "edited": t["edited"][:2], "self_recorded": t["self_recorded"]})
    return out


# ─── offline ─────────────────────────────────────────────────────────────

def transcript_cwd(path) -> str:
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


def dry_run(cfg, files) -> int:
    """Every transcript, nothing written: what the extractor would do."""
    total = {"transcripts": 0, "triads": 0, "would_write": 0, "self_recorded": 0, "no_dest": 0, "capped": 0}
    for f in files:
        total["transcripts"] += 1
        cwd = transcript_cwd(f)
        sid = Path(f).stem
        try:
            day = datetime.fromtimestamp(Path(f).stat().st_mtime).strftime("%Y-%m-%d")
        except OSError:
            day = "????-??-??"
        try:
            r = run(f, sid, cwd, cfg, datetime.strptime(day, "%Y-%m-%d"), dry=True)
        except (OSError, ValueError):
            continue
        if not r["triads"]:
            continue
        total["triads"] += r["triads"]
        print(f"=== {Path(cwd).name or '?'}  sid {sid[:8]}  {day}  ({r['triads']} triad(s))")
        for l in r["lines"]:
            status = l["status"]
            if status == "dry-run":
                total["would_write"] += 1
                status = "write"
            elif status == "self-recorded":
                total["self_recorded"] += 1
            elif status == "no-destination":
                total["no_dest"] += 1
            elif status == "capped":
                total["capped"] += 1
            where = f"{l['kind']}: {l['path']}" if l["kind"] else "(no destination)"
            print(f"  [{status}] -> {where}")
            print(f"    {l['line']}")
            print(f"    user: {' '.join(l['prompt'].split())[:160]}")
            print(f"    model: «{l['admission']}»; edited {', '.join(Path(p).name for p in l['edited'])}")
        print()
    print(f"transcripts {total['transcripts']}, triads {total['triads']}, would write {total['would_write']}, "
          f"self-recorded {total['self_recorded']}, no destination {total['no_dest']}, capped {total['capped']}")
    return 0


def main() -> int:
    import glob
    cfg = load_config()
    a = sys.argv[1:]
    if "--pending" in a:
        rows = read_jsonl(PENDING)
        print(f"{len(rows)} pending")
        for r in rows:
            print(f"  {r.get('repo')}: {r.get('line')}")
        return 0
    if "--dry-run" in a:
        files = [x for x in a if not x.startswith("--")]
        if not files:
            files = sorted(f for f in glob.glob(str(cfg["transcripts"] / "*" / "*.jsonl"))
                           if Path(f).parent.name != project_folder_name(WORKER_DIR))
        return dry_run(cfg, files)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
