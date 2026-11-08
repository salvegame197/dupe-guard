#!/usr/bin/env python3
"""
ksearch — search accumulated knowledge: Claude Code transcripts, the vault and
the native memory Claude Code keeps per project folder.

Transcripts hold almost all the real work, but they are unreadable in practice:
grep returns JSON blobs, and "632 sessions match" is not an answer. Here the
result comes out readable, ranked and deduplicated.

A raw scan costs seconds, too slow to use interactively, so the readable text
is extracted ONCE into a compact index and searches read only the index.

No LLM: indexing and search are deterministic and offline.

Usage:
    ksearch.py --reindex              # (re)build the index
    ksearch.py --reindex --with-questions   # also ingest brain/questions.py output (offline self-test)
    ksearch.py payment refund         # search (every term counts)
    ksearch.py payment --json         # output for an agent
    ksearch.py payment --limit 5 --project billing-api
    ksearch.py catraca --source memory   # only ~/.claude/projects/*/memory/*.md
"""

import argparse
import json
import math
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import (DATA, WORKER_DIR, fold, is_dataless, load_config,      # noqa: E402
                        project_folder_name, redact)
from brain.distill import CHECKLIST, INSTRUCTIONS                              # noqa: E402

INDEX = DATA / "ksearch-index.jsonl"
# The opening words of the prompts the brain itself sends to `claude -p`. A
# transcript that starts with one of them is a worker's, not a person's: 94
# of them were in the index as "sessions", each carrying a whole note.
WORKER_PROMPTS = tuple(" ".join(p[:60].split()) for p in (INSTRUCTIONS, CHECKLIST))
STOPWORDS = DATA / "ksearch-stopwords.json"
ASSOC = DATA / "ksearch-assoc.json"
MAX_TURN_CHARS = 2000
SNIPPET_CHARS = 220
WEAK_SCORE = 25.0   # below this the literal search disappointed -> try synonyms


# ─── normalisation ───────────────────────────────────────────────────────

def clean(text: str) -> str:
    """Drop what is not conversation: long code blocks, base64."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"[A-Za-z0-9+/]{120,}={0,2}", " ", text)
    return " ".join(text.split())


def is_synthetic(body: str) -> bool:
    """Messages injected by the harness — not what the person said."""
    if not body:
        return True
    if body.startswith(("<", "[Request interrupted", "Base directory for this skill:")):
        return True
    if body.startswith("#") and "\n## " in body and len(body) > 300:
        return True
    if body.startswith(WORKER_PROMPTS):
        return True
    return False


def is_worker_transcript(path: Path, cwd: str = "") -> bool:
    """A transcript written by the brain's own `claude -p` child: it lives in
    the project folder of WORKER_DIR, or records that folder as its cwd."""
    if path.parent.name == project_folder_name(WORKER_DIR):
        return True
    return bool(cwd) and Path(cwd) == WORKER_DIR


def text_of(message) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        c = message.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return "\n".join(b.get("text", "") for b in c
                             if isinstance(b, dict) and b.get("type") == "text")
    return ""


# ─── indexing ────────────────────────────────────────────────────────────

def index_transcript(path: Path):
    meta = {"title": None, "project": None, "branch": None, "date": None}
    turns = []
    try:
        with path.open(encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                kind = row.get("type")
                if kind == "ai-title":
                    meta["title"] = meta["title"] or row.get("aiTitle")
                    continue
                if kind not in ("user", "assistant"):
                    continue
                meta["project"] = meta["project"] or row.get("cwd")
                if is_worker_transcript(path, meta["project"] or ""):
                    return None       # the brain talking to itself, not a session
                meta["branch"] = meta["branch"] or row.get("gitBranch")
                ts = row.get("timestamp")
                if ts and (meta["date"] is None or ts > meta["date"]):
                    meta["date"] = ts
                # Redacted before anything is kept: the index lives outside
                # ~/.claude and is read on every prompt.
                body = clean(redact(text_of(row.get("message"))))
                if kind == "user":
                    if body.startswith(WORKER_PROMPTS) and not any(t["r"] == "u" for t in turns):
                        return None   # first prompt is the brain's own: a worker child
                    if is_synthetic(body):
                        continue
                elif len(body) < 40:
                    continue  # short assistant noise ("Ok.", "Done.")
                if body:
                    t = body[:MAX_TURN_CHARS]
                    turns.append({"r": kind[0], "t": t, "f": fold(t)})
    except OSError:
        return None
    if not turns:
        return None
    return {
        "src": "transcript",
        "id": path.stem,
        "title": meta["title"] or "(untitled)",
        "project": (meta["project"] or "").split("/")[-1],
        "branch": meta["branch"],
        "date": (meta["date"] or "")[:10],
        "turns": turns,
    }


def index_vault(path: Path, root: Path):
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    body = re.sub(r"^---\n.*?\n---\n", "", raw, flags=re.S)
    if len(clean(body)) < 50:
        return None
    # One turn per SECTION, not one per page. A page indexed as a single blob
    # was cut at MAX_TURN_CHARS * 4 (long project notes lost their tail) and
    # scored by density, which punishes length: a two-line chat outscored the
    # note that actually held the answer. Sections keep the density honest
    # and the snippet lands on the part that matched.
    turns = []
    for chunk in re.split(r"(?=^#{1,3} )", body, flags=re.M):
        text = clean(chunk)
        if len(text) >= 40:
            t = text[:MAX_TURN_CHARS * 2]
            turns.append({"r": "v", "t": t, "f": fold(t)})
    if not turns:
        return None
    rel = path.relative_to(root)
    try:
        date = time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
    except OSError:
        date = None
    return {
        "src": "vault",
        "id": str(rel),
        "title": path.stem,
        "project": str(rel.parent) if rel.parent != Path(".") else "vault",
        "branch": None,
        "date": date,
        "turns": turns,
    }


MEMORY_INDEX_FILE = "MEMORY.md"       # the per-folder index Claude Code keeps; not a note


def memory_frontmatter(raw: str):
    """(fields, body) of a native memory file. The frontmatter is `name`,
    `description` and a nested `metadata:` block with `type` (feedback,
    project, reference, user) and `modified`; a few older files carry `type`
    at the top level. Read with regexes: no YAML parser in a hook."""
    m = re.match(r"---\n(.*?)\n---\n?", raw, flags=re.S)
    if not m:
        return {}, raw
    fm, body = m.group(1), raw[m.end():]
    fields = {}
    for key in ("name", "description", "type", "modified"):
        k = re.search(rf"^\s*{key}:\s*(.+?)\s*$", fm, flags=re.M)
        if k:
            fields[key] = k.group(1).strip().strip("\"'")
    return fields, body


def unfold_folder(folder: str, home: Path = None):
    """The directory a project folder name stands for: from the home folder
    down, each child whose own folded name ('API-Producer-NODE' ->
    'API-Producer-NODE', 'ancora_v1' -> 'ancora-v1') is the next piece of
    the name is followed. Only for folders whose transcripts have all
    expired, at index time; None when nothing on disk matches."""
    home = home or Path.home()
    prefix = project_folder_name(home) + "-"
    if not folder.startswith(prefix):
        return None
    stack = [(home, folder[len(prefix):])]
    while stack:
        p, rest = stack.pop()
        if not rest:
            return p
        try:
            children = [c for c in p.iterdir() if c.is_dir()]
        except OSError:
            continue
        for c in children:
            name = project_folder_name(c.name)
            if rest == name:
                stack.append((c, ""))
            elif rest.startswith(name + "-"):
                stack.append((c, rest[len(name) + 1:]))
    return None


def memory_label(folder: str, cwd_by_folder: dict) -> str:
    """What to call the project a memory folder belongs to: the basename of
    the cwd its transcripts record ('shop-admin'); '~' for the home folder;
    when every transcript has expired, the basename of the directory the
    folder name unfolds to; failing that, the name without the home prefix."""
    if folder == project_folder_name(Path.home()):
        return "~"
    label = cwd_by_folder.get(folder)
    if label:
        return label
    found = unfold_folder(folder)
    if found is not None:
        return found.name
    prefix = project_folder_name(Path.home()) + "-"
    return folder[len(prefix):] if folder.startswith(prefix) else folder.lstrip("-")


def index_memory(path: Path, folder: str, label: str):
    """One native memory file (~/.claude/projects/<folder>/memory/<name>.md)
    as a document: id `memory/<folder>/<name>.md`, title from `name`, the
    description as the first section (it is the subject in one line), then
    the body by section like a vault page. `kind` carries the memory type."""
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    fields, body = memory_frontmatter(raw)
    name = fields.get("name") or path.stem
    desc = clean(fields.get("description") or "")
    turns = []
    if len(desc) >= 10:
        turns.append({"r": "v", "t": desc, "f": fold(desc)})
    for chunk in re.split(r"(?=^#{1,3} )", body, flags=re.M):
        text = clean(chunk)
        if len(text) >= 40:
            t = text[:MAX_TURN_CHARS * 2]
            turns.append({"r": "v", "t": t, "f": fold(t)})
    if not turns:
        return None
    date = (fields.get("modified") or "")[:10]
    if not re.match(r"\d{4}-\d{2}-\d{2}$", date):
        try:
            date = time.strftime("%Y-%m-%d", time.localtime(path.stat().st_mtime))
        except OSError:
            date = None
    return {
        "src": "memory",
        "id": f"memory/{folder}/{path.name}",
        "title": name,
        "desc": desc,
        "kind": fields.get("type") or "project",
        "project": label,
        "branch": None,
        "date": date,
        "turns": turns,
    }


def memory_files(transcripts: Path):
    """(path, folder) of every native memory note, MEMORY.md and the brain's
    own worker folder left out."""
    if not transcripts.is_dir():
        return
    worker = project_folder_name(WORKER_DIR)
    for folder in sorted(transcripts.iterdir()):
        if folder.name == worker or not (folder / "memory").is_dir():
            continue
        for p in sorted((folder / "memory").glob("*.md")):
            if p.name != MEMORY_INDEX_FILE and p.is_file():
                yield p, folder.name


def excluded_vault_paths(cfg) -> set:
    """Vault files and folders that must not be searchable, resolved:
      - the truth page holds the evaluation questions; indexed, it answers
        them and the score lies;
      - Sessions/Raw is the session log itself, a digest of the transcripts
        already indexed, so it only duplicated them;
      - the proposals page is a list of checkboxes about the harness, 78 KB
        of it, rewritten every session; nothing a prompt should recall."""
    vault = Path(cfg["vault"])
    out = set()
    for key in ("truth_page", "proposals_page", "raw_dir"):
        if cfg.get(key):
            out.add(str((vault / cfg[key]).resolve()))
    return out


def is_excluded(path: Path, excluded: set) -> bool:
    try:
        r = path.resolve()
    except OSError:
        return False
    return any(str(r) == x or str(r).startswith(x + "/") for x in excluded)


def build_index(with_questions: bool = False) -> int:
    cfg = load_config()
    started = time.time()
    written = 0
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")

    memories = 0
    with tmp.open("w", encoding="utf-8") as out:
        cwd_by_folder = {}
        if cfg["transcripts"].is_dir():
            for p in cfg["transcripts"].rglob("*.jsonl"):
                if is_worker_transcript(p):
                    continue
                doc = index_transcript(p)
                if doc:
                    doc["tf"] = fold(doc["title"])
                    out.write(json.dumps(doc, ensure_ascii=False) + "\n")
                    written += 1
                    if doc["project"]:
                        cwd_by_folder.setdefault(p.parent.name, doc["project"])
        # The native memory Claude Code writes per project folder: the rule
        # taught in one repo is invisible from a session in another, and
        # that is exactly what a cross-repo recall is for. The folded title
        # includes the description, so its words count as title hits.
        for p, folder in memory_files(cfg["transcripts"]):
            doc = index_memory(p, folder, memory_label(folder, cwd_by_folder))
            if doc:
                doc["tf"] = fold(f"{doc['title']} {doc['desc']}")
                out.write(json.dumps(doc, ensure_ascii=False) + "\n")
                written += 1
                memories += 1
        vault = cfg["vault"]
        skip = excluded_vault_paths(cfg)
        evicted = 0
        if vault.is_dir():
            for p in vault.rglob("*.md"):
                if any(part.startswith((".", "data-")) for part in p.relative_to(vault).parts):
                    continue
                if is_excluded(p, skip):
                    continue
                if is_dataless(p):
                    evicted += 1          # not on disk: reading would block on the cloud
                    continue
                doc = index_vault(p, vault)
                if doc:
                    doc["tf"] = fold(doc["title"])
                    out.write(json.dumps(doc, ensure_ascii=False) + "\n")
                    written += 1

    # Questions the worker wrote for each block (brain/questions.py) join the
    # document as turns of kind "q", so a stored question can match the
    # user's word for word. Only on request: the store (2 MB, written once
    # in September) is an offline self-test now, not part of what every
    # prompt reads; the automatic reindex never passes the flag.
    qmap = {}
    if with_questions:
        try:
            from brain.questions import by_doc
            qmap = by_doc()
        except Exception:
            qmap = {}
    if qmap:
        lines = tmp.read_text(encoding="utf-8").splitlines()
        with tmp.open("w", encoding="utf-8") as out:
            for line in lines:
                doc = json.loads(line)
                for q in qmap.get(doc["id"], [])[:40]:
                    doc["turns"].append({"r": "q", "t": q, "f": fold(q)})
                out.write(json.dumps(doc, ensure_ascii=False) + "\n")
    tmp.replace(INDEX)  # atomic swap: a concurrent search never sees half an index
    size = INDEX.stat().st_size / 1024 / 1024
    print(f"indexed {written} documents ({memories} native memory notes) in "
          f"{time.time() - started:.1f}s ({size:.1f} MB in {INDEX})", file=sys.stderr)
    if evicted:
        print(f"{evicted} vault page(s) skipped: not on disk (cloud placeholder). "
              f"On macOS: brctl download \"{vault}\"", file=sys.stderr)
    n = build_stopwords()
    print(f"{n} non-discriminating words derived from the corpus ({STOPWORDS})",
          file=sys.stderr)
    a = build_assoc()
    print(f"{a} terms with domain synonyms derived from the corpus ({ASSOC})",
          file=sys.stderr)
    return 0


def build_assoc(min_df: int = 8, max_ratio: float = 0.10,
                min_pair: int = 6, top_k: int = 6) -> int:
    """
    Derive DOMAIN synonyms from the corpus itself, with no LLM and no thesaurus.

    If 'ticket' and 'voucher' appear together far more than chance explains,
    they are the same thing in this person's vocabulary. That is PMI (pointwise
    mutual information), the same statistics as IDF turned around: instead of
    "which word does not discriminate", "which words travel together". No
    dictionary knows a team's jargon; the corpus does.

    Guards: a term too rare (min_df) gives unstable PMI; too common
    (max_ratio) associates with everything; a pair seen few times (min_pair)
    is noise.
    """
    # Co-occurrence per TURN, not per document. Per document the association
    # turns to garbage, because one session touches ten subjects and everything
    # co-occurs with everything. Within a turn, two words together almost
    # always talk about the same thing.
    docs_terms = []
    df = Counter()
    with INDEX.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            for turn in doc["turns"]:
                words = set(re.findall(r"[a-z][a-z]{3,}", fold(turn["t"])))
                if len(words) < 3:
                    continue
                docs_terms.append(words)
                df.update(words)

    n = len(docs_terms)
    if n < 200:
        ASSOC.write_text("{}", encoding="utf-8")
        return 0

    vocab = {w for w, c in df.items() if min_df <= c <= n * max_ratio}
    if not vocab:
        return 0

    pair = Counter()
    for words in docs_terms:
        present = sorted(words & vocab)       # sorted makes (a,b) == (b,a)
        if len(present) > 120:                # a turn about everything associates nothing
            continue
        for i, a in enumerate(present):
            for b in present[i + 1:]:
                pair[(a, b)] += 1

    assoc = defaultdict(list)
    for (a, b), c in pair.items():
        if c < min_pair:
            continue
        # normalised PMI stays in [-1, 1] and does not favour rare terms
        p_ab, p_a, p_b = c / n, df[a] / n, df[b] / n
        npmi = math.log(p_ab / (p_a * p_b)) / -math.log(p_ab)
        if npmi <= 0.30:
            continue
        assoc[a].append((npmi, b))
        assoc[b].append((npmi, a))

    out = {}
    for term, cands in assoc.items():
        cands.sort(reverse=True)
        out[term] = [w for _, w in cands[:top_k]]
    ASSOC.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return len(out)


MIN_DOCS_FOR_STOPWORDS = 50


def build_stopwords(threshold: float = 0.25) -> int:
    """
    Derive from the corpus the words that discriminate nothing.

    A hand-written list is never enough: long verbs pass any length filter and
    nobody guesses they need blocking. The corpus answers on its own: a word
    present in more than 25% of documents separates nothing, which is the
    principle of IDF. Written once at indexing time; consumers just load it.

    Only from a real corpus, though. With a handful of documents every word
    is in more than 25% of them, so a fresh vault turned every word into a
    stopword and recall went silent without a single error.
    """
    df = Counter()
    docs = 0
    with INDEX.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                doc = json.loads(line)
            except json.JSONDecodeError:
                continue
            docs += 1
            words = set()
            for turn in doc["turns"]:
                if turn.get("r") == "q":
                    continue        # synthetic questions repeat the same words
                                    # everywhere and would turn real terms into noise
                words.update(re.findall(r"[a-z][a-z]{2,}", turn.get("f") or fold(turn["t"])))
            df.update(words)
    if docs < MIN_DOCS_FOR_STOPWORDS:
        STOPWORDS.write_text("[]", encoding="utf-8")
        return 0
    cutoff = docs * threshold
    common = sorted(w for w, c in df.items() if c >= cutoff)
    STOPWORDS.write_text(json.dumps(common, ensure_ascii=False), encoding="utf-8")
    return len(common)


# ─── search ──────────────────────────────────────────────────────────────

def load_index():
    if not INDEX.exists():
        print("no index yet — run: ksearch.py --reindex", file=sys.stderr)
        sys.exit(1)
    docs = []
    with INDEX.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                docs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return docs


def expand(terms, limit=3):
    """
    Build synonym groups: each term becomes {original + associates}.

    Matching any member of a group counts as matching the term, which is the
    right semantics for a synonym. Adding synonyms to a flat list would be
    wrong: it dilutes coverage (1 of 4 "terms" when it is really 1 of 1 idea).
    """
    try:
        table = json.loads(ASSOC.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [[t] for t in terms]
    return [[t] + table.get(t, [])[:limit] for t in terms]


def score_doc(doc, groups):
    """
    Score a document and pick its best snippets.

      1. The TITLE weighs a lot. It is the topic already distilled; matching
         there says more about the session than any count in the body.
      2. DENSITY, not count. 5 hits in a 2000-char blob mean less than 2 in a
         100-char prompt. Without normalising by length, a long explanation
         always beats a short, precise question.
      3. Weight by origin: the user's intent > the assistant's explanation.
    """
    terms_n = len(groups)
    total = 0.0

    title_folded = doc.get("tf") or fold(doc.get("title") or "")
    title_hits = sum(1 for g in groups if any(t in title_folded for t in g))
    if title_hits:
        # Quadratic in coverage: every term in the title is the strongest
        # signal there is and should beat any volume in the body.
        total += 40.0 * (title_hits ** 2) / terms_n

    hits = []
    qhit = 0.0
    for turn in doc["turns"]:
        text = turn["t"]
        # folded at index time: folding every block on every query cost
        # ~700 ms once pages were split into sections
        folded = turn.get("f") or fold(text)
        matched = sum(1 for g in groups if any(t in folded for t in g))
        if not matched:
            continue
        occurrences = sum(folded.count(t) for g in groups for t in g)
        # density per 500 chars, capped, so a tiny text with one hit does not
        # jump to the top
        density = min(occurrences / max(len(text) / 500, 1.0), 4.0)
        weight = 3.0 if turn["r"] in ("u", "v") else 1.0
        coverage = (matched / terms_n) ** 2
        if turn["r"] == "q":
            # a stored question matching most of the terms is the subject
            # distilled, like a title: count it as one
            if matched >= max(2, (terms_n + 1) // 2):
                qhit = max(qhit, coverage)
            continue
        hits.append((weight * coverage * (1.0 + density), matched, occurrences, turn))

    if qhit:
        total += 40.0 * qhit
    if not hits and not total:
        return None

    # Summing EVERY turn rewarded long sessions: 200 turns that only brush the
    # subject beat a short session entirely about it. Summing the best few,
    # concentration wins over volume.
    hits.sort(key=lambda h: -h[0])
    total += sum(h[0] for h in hits[:5])
    if not total:
        return None
    best = sorted(hits[:5], key=lambda h: (-h[1], -h[2]))
    doc["_qhit"] = qhit
    return total, [h[3] for h in best[:3]]


def snippet(text, terms):
    """Cut around the first occurrence, without splitting a word."""
    folded = fold(text)
    pos = min((folded.find(t) for t in terms if folded.find(t) >= 0), default=0)
    start = max(0, pos - SNIPPET_CHARS // 3)
    end = min(len(text), start + SNIPPET_CHARS)
    out = text[start:end].strip()
    if start > 0:
        out = "..." + out.split(" ", 1)[-1] if " " in out else out
    if end < len(text):
        out = (out.rsplit(" ", 1)[0] if " " in out else out) + "..."
    return out


def search(terms, limit=10, project=None, source=None, no_expand=False, synonyms=None):
    """(results, expanded) with results = [(score, doc, best_turns)], ranked.
    `synonyms` maps a term to phrases that count as matching it (a repo folder
    to the title of its note, for instance); they join the term's group."""
    docs = load_index()
    synonyms = synonyms or {}

    def run(groups):
        found, seen_sig = [], set()
        for doc in docs:
            if source and doc["src"] != source:
                continue
            if project and project.lower() not in (doc["project"] or "").lower():
                continue
            scored = score_doc(doc, groups)
            if not scored:
                continue
            total, best = scored
            if not best:
                best = [t for t in doc["turns"] if t["r"] != "q"][:1]
                if not best:
                    continue
            sig = fold(best[0]["t"])[:150]
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            found.append((total, doc, best))
        found.sort(key=lambda r: -r[0])
        return found

    results = run([[t] + [fold(x) for x in synonyms.get(t, [])] for t in terms])
    expanded = False
    # Expansion is CONSERVATIVE: only when the literal search disappoints.
    # Statistical synonyms are right for some terms and wrong for others;
    # always using them would trade precision for coverage on every search.
    if not no_expand and (len(results) < 3 or results[0][0] < WEAK_SCORE):
        groups = expand(terms)
        if any(len(g) > 1 for g in groups):
            results = run(groups)
            expanded = True
    return results, expanded


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*", help="search terms")
    ap.add_argument("--reindex", action="store_true")
    ap.add_argument("--with-questions", action="store_true",
                    help="with --reindex: ingest the stored questions (offline self-test only)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--project", help="filter by project substring")
    ap.add_argument("--source", choices=["transcript", "vault", "memory"], help="filter by origin")
    ap.add_argument("--json", action="store_true", help="output for an agent")
    ap.add_argument("--no-expand", action="store_true", help="disable synonyms")
    args = ap.parse_args()

    if args.reindex:
        return build_index(with_questions=args.with_questions)
    if not args.query:
        ap.print_help()
        return 2

    terms = [fold(t) for t in args.query]
    started = time.time()
    results, expanded = search(terms, args.limit, args.project, args.source, args.no_expand)
    top = results[: args.limit]
    elapsed = time.time() - started

    if args.json:
        print(json.dumps({
            "query": args.query,
            "expanded": expanded,
            "matched": len(results),
            "results": [
                {"score": round(s, 1), "source": d["src"], "title": d["title"],
                 "project": d["project"], "date": d["date"], "id": d["id"],
                 "snippets": [snippet(t["t"], terms) for t in b]}
                for s, d, b in top
            ],
        }, ensure_ascii=False, indent=2))
        return 0

    if not results:
        print(f"nothing found for: {' '.join(args.query)}")
        return 0

    tag = " [+synonyms]" if expanded else ""
    print(f"{len(results)} result(s) for '{' '.join(args.query)}'{tag} "
          f"— showing {len(top)} ({elapsed * 1000:.0f}ms)\n")
    for rank, (total, doc, best) in enumerate(top, 1):
        mark = {"vault": "V", "memory": "M"}.get(doc["src"], " ")
        where = doc["project"] or "?"
        when = f"  {doc['date']}" if doc["date"] else ""
        print(f"{rank:2}.{mark} [{where}]{when}  {doc['title']}   ·{total:.0f}")
        for turn in best:
            who = {"u": "you", "a": "claude", "v": "vault"}[turn["r"]]
            print(f"      {who}: {snippet(turn['t'], terms)}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
