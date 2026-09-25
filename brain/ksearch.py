#!/usr/bin/env python3
"""
ksearch — search accumulated knowledge: Claude Code transcripts and the vault.

Transcripts hold almost all the real work, but they are unreadable in practice:
grep returns JSON blobs, and "632 sessions match" is not an answer. Here the
result comes out readable, ranked and deduplicated.

A raw scan costs seconds, too slow to use interactively, so the readable text
is extracted ONCE into a compact index and searches read only the index.

No LLM: indexing and search are deterministic and offline.

Usage:
    ksearch.py --reindex              # (re)build the index
    ksearch.py payment refund         # search (every term counts)
    ksearch.py payment --json         # output for an agent
    ksearch.py payment --limit 5 --project billing-api
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
from brain.core import DATA, fold, load_config                  # noqa: E402

INDEX = DATA / "ksearch-index.jsonl"
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
    return False


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
                meta["branch"] = meta["branch"] or row.get("gitBranch")
                ts = row.get("timestamp")
                if ts and (meta["date"] is None or ts > meta["date"]):
                    meta["date"] = ts
                body = clean(text_of(row.get("message")))
                if kind == "user":
                    if is_synthetic(body):
                        continue
                elif len(body) < 40:
                    continue  # short assistant noise ("Ok.", "Done.")
                if body:
                    turns.append({"r": kind[0], "t": body[:MAX_TURN_CHARS]})
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
    body = clean(re.sub(r"^---\n.*?\n---\n", "", raw, flags=re.S))
    if len(body) < 50:
        return None
    rel = path.relative_to(root)
    return {
        "src": "vault",
        "id": str(rel),
        "title": path.stem,
        "project": str(rel.parent) if rel.parent != Path(".") else "vault",
        "branch": None,
        "date": None,
        "turns": [{"r": "v", "t": body[: MAX_TURN_CHARS * 4]}],
    }


def build_index() -> int:
    cfg = load_config()
    started = time.time()
    written = 0
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")

    with tmp.open("w", encoding="utf-8") as out:
        if cfg["transcripts"].is_dir():
            for p in cfg["transcripts"].rglob("*.jsonl"):
                doc = index_transcript(p)
                if doc:
                    out.write(json.dumps(doc, ensure_ascii=False) + "\n")
                    written += 1
        vault = cfg["vault"]
        if vault.is_dir():
            for p in vault.rglob("*.md"):
                if any(part.startswith((".", "data-")) for part in p.relative_to(vault).parts):
                    continue
                doc = index_vault(p, vault)
                if doc:
                    out.write(json.dumps(doc, ensure_ascii=False) + "\n")
                    written += 1

    tmp.replace(INDEX)  # atomic swap: a concurrent search never sees half an index
    size = INDEX.stat().st_size / 1024 / 1024
    print(f"indexed {written} documents in {time.time() - started:.1f}s "
          f"({size:.1f} MB in {INDEX})", file=sys.stderr)
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


def build_stopwords(threshold: float = 0.25) -> int:
    """
    Derive from the corpus the words that discriminate nothing.

    A hand-written list is never enough: long verbs pass any length filter and
    nobody guesses they need blocking. The corpus answers on its own: a word
    present in more than 25% of documents separates nothing, which is the
    principle of IDF. Written once at indexing time; consumers just load it.
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
                words.update(re.findall(r"[a-z][a-z]{2,}", fold(turn["t"])))
            df.update(words)
    if not docs:
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

    title_folded = fold(doc.get("title") or "")
    title_hits = sum(1 for g in groups if any(t in title_folded for t in g))
    if title_hits:
        # Quadratic in coverage: every term in the title is the strongest
        # signal there is and should beat any volume in the body.
        total += 40.0 * (title_hits ** 2) / terms_n

    hits = []
    for turn in doc["turns"]:
        text = turn["t"]
        folded = fold(text)
        matched = sum(1 for g in groups if any(t in folded for t in g))
        if not matched:
            continue
        occurrences = sum(folded.count(t) for g in groups for t in g)
        # density per 500 chars, capped, so a tiny text with one hit does not
        # jump to the top
        density = min(occurrences / max(len(text) / 500, 1.0), 4.0)
        weight = 3.0 if turn["r"] in ("u", "v") else 1.0
        coverage = (matched / terms_n) ** 2
        hits.append((weight * coverage * (1.0 + density), matched, occurrences, turn))

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


def search(terms, limit=10, project=None, source=None, no_expand=False):
    """(results, expanded) with results = [(score, doc, best_turns)], ranked."""
    docs = load_index()

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
                # A title-only hit has no snippet to deduplicate on; best[0]
                # used to raise here and silently take recall down with it.
                continue
            sig = fold(best[0]["t"])[:150]
            if sig in seen_sig:
                continue
            seen_sig.add(sig)
            found.append((total, doc, best))
        found.sort(key=lambda r: -r[0])
        return found

    results = run([[t] for t in terms])
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
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--project", help="filter by project substring")
    ap.add_argument("--source", choices=["transcript", "vault"], help="filter by origin")
    ap.add_argument("--json", action="store_true", help="output for an agent")
    ap.add_argument("--no-expand", action="store_true", help="disable synonyms")
    args = ap.parse_args()

    if args.reindex:
        return build_index()
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
        mark = "V" if doc["src"] == "vault" else " "
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
