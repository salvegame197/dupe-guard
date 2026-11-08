#!/usr/bin/env python3
"""
questions — a worker that reads every block and writes the questions it
answers.

Paraphrase is where keyword search dies: "relogio" for a page titled "Apple
Watch". Vectors were tried and did not pay (one more hit in thirty, for
+130 ms on every prompt). This attacks it from the other side, ahead of
time: a small local model reads each section once and writes the questions
a person would ask that the section answers. Those questions are indexed as
text next to the section, so the user's question matches a stored question
word for word, with the same fast keyword search. The meaning is computed
while nobody is waiting.

Runs for hours in the background (a few seconds per block on a laptop) and
saves after every block, so an interrupted run resumes and a partial run is
already useful. Blocks are keyed by content hash: unchanged text is never
re-read.

OFFLINE since 2026-11-08. The store is not part of the production index any
more and the automatic reindex never reads it: it was written once (26/09),
never refreshed, and added 2 MB to what every prompt loads. To run the
self-test, put the questions in the index on purpose, evaluate, and let the
next automatic reindex (or a plain --reindex) drop them again:

    python3 brain/ksearch.py --reindex --with-questions
    python3 brain/questions.py --eval 100
    python3 brain/ksearch.py --reindex

Usage:
    questions.py --run [--limit N] [--prefix Folder/]   generate for blocks that lack questions
    questions.py --stats
    questions.py --eval [N]            ask N stored questions back to recall:
                                       does the block come back? (self-test;
                                       needs --reindex --with-questions first)
"""
import hashlib
import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, load_config, settings as core_settings       # noqa: E402

STORE = DATA / "questions.jsonl"
LOG = DATA / "questions.log"
MAX_CHARS = 1800

# Kept plain on purpose: a small model parrots an elaborate instruction back
# instead of asking. Everyday-word paraphrase is a separate, one-word question
# (see generic()) that only a larger model answers reliably.
PROMPTS = {
    "en": ("Read the passage and write {n} short questions, in English, that a person would ask "
           "and that this passage answers. One per line, no numbering, no explanation.\n\nPASSAGE:\n"),
    "pt": ("Leia o trecho e escreva {n} perguntas curtas, em portugues, que uma pessoa faria e que "
           "este trecho responde. Uma por linha, sem numeracao, sem explicacao.\n\nTRECHO:\n"),
}


def settings(cfg):
    return core_settings(cfg, "questions", {"enabled": False, "url": "http://localhost:11434", "model": "qwen2.5:3b",
                                            "per_unit": 3, "sources": ["vault"], "seconds_budget": 0})


def generate(text, cfg):
    s = settings(cfg)
    lang = cfg["languages"][0] if cfg["languages"][0] in PROMPTS else "en"
    prompt = PROMPTS[lang].format(n=s["per_unit"]) + text[:MAX_CHARS]
    req = urllib.request.Request(f"{s['url'].rstrip('/')}/api/generate",
                                 data=json.dumps({"model": s["model"], "prompt": prompt, "stream": False,
                                                  "options": {"temperature": 0.3, "num_predict": 60 * s["per_unit"]},
                                                  "keep_alive": "30m"}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            out = json.loads(r.read())["response"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
    qs = []
    for line in out.splitlines():
        q = line.strip().lstrip("-*0123456789.) ").strip()
        if 12 <= len(q) <= 200 and "?" in q and not q.lower().startswith(("como as pessoas", "como voce fala", "how people", "how would you say")):
            qs.append(q)
    return qs[: s["per_unit"]]


def units(cfg):
    """(unit_id, doc_id, title, text) for the sources selected, curated
    folders first: they are the ones a question is most likely about."""
    from brain import ksearch
    s = settings(cfg)
    trusted = tuple(cfg.get("trusted_prefixes") or ())
    out = []
    for doc in ksearch.load_index():
        if doc["src"] not in s["sources"]:
            continue
        for i, t in enumerate(doc["turns"]):
            if t.get("r") == "q" or len(t["t"]) < 60:
                continue
            out.append((f"{doc['id']}#{i}", doc["id"], doc["title"], t["t"]))
    out.sort(key=lambda u: (0 if u[1].startswith(trusted) else 1, u[1]))
    return out


def load_store():
    got = {}
    if STORE.exists():
        for l in STORE.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(l)
                got[d["unit"]] = d
            except (ValueError, KeyError):
                continue
    return got


def run(cfg, limit=0, prefix=""):
    s = settings(cfg)
    have = load_store()
    todo = [(u, d, ti, tx) for u, d, ti, tx in units(cfg)
            if d.startswith(prefix) and have.get(u, {}).get("hash") != hashlib.sha1(tx.encode()).hexdigest()[:16]]
    if limit:
        todo = todo[:limit]
    print(f"{len(have)} blocks done, {len(todo)} to do", flush=True)
    DATA.mkdir(parents=True, exist_ok=True)
    started, n = time.time(), 0
    with STORE.open("a", encoding="utf-8") as f:
        for u, d, title, text in todo:
            if s["seconds_budget"] and time.time() - started > s["seconds_budget"]:
                break
            qs = generate(f"{title}\n{text}", cfg)
            if qs is None:
                print("model server not reachable; stopping.", file=sys.stderr)
                return 1
            f.write(json.dumps({"unit": u, "doc": d, "hash": hashlib.sha1(text.encode()).hexdigest()[:16],
                                "questions": qs, "ts": int(time.time())}, ensure_ascii=False) + "\n")
            f.flush()
            n += 1
            if n % 25 == 0:
                rate = (time.time() - started) / n
                print(f"  {n}/{len(todo)}  {rate:.1f}s per block, ~{(len(todo) - n) * rate / 60:.0f} min left", flush=True)
    print(f"generated for {n} block(s) in {(time.time() - started) / 60:.1f} min")
    return 0


def by_doc():
    """{doc_id: [questions]} — what the index ingests."""
    out = {}
    for d in load_store().values():
        out.setdefault(d["doc"], []).extend(d.get("questions") or [])
    return out


def evaluate(cfg, n=100):
    """Self-test: ask stored questions back to recall; the block's document
    should be among what comes back. The user's thirty questions are the
    anchor; this is the trend, nightly, without anyone writing questions."""
    from brain import ksearch, recall
    if not any(t.get("r") == "q" for d in ksearch.load_index() for t in d["turns"]):
        print("the index holds no stored questions; run first: ksearch.py --reindex --with-questions",
              file=sys.stderr)
        return 1
    store = list(load_store().values())
    random.seed(int(time.time()) // 86400)         # a fresh sample per day, stable within it
    sample = [d for d in random.sample(store, min(n, len(store))) if d.get("questions")]
    hits = tot = 0
    for d in sample:
        q = random.choice(d["questions"])
        _, _, picked = recall.recall(q, "", cfg)
        tot += 1
        hits += any(p["id"] == d["doc"] for p in picked)
    pct = 100 * hits / tot if tot else 0
    print(f"self-test: {hits}/{tot} stored questions bring their own block back ({pct:.0f}%)")
    try:
        with (DATA / "questions-eval.jsonl").open("a") as f:
            f.write(json.dumps({"ts": int(time.time()), "hits": hits, "total": tot}) + "\n")
    except OSError:
        pass
    return 0


def main() -> int:
    cfg = load_config()
    a = sys.argv[1:]
    if a[:1] == ["--run"]:
        limit = int(a[a.index("--limit") + 1]) if "--limit" in a else 0
        prefix = a[a.index("--prefix") + 1] if "--prefix" in a else ""
        return run(cfg, limit, prefix)
    if a[:1] == ["--stats"]:
        st = load_store()
        print(f"{len(st)} blocks with questions, {sum(len(d.get('questions') or []) for d in st.values())} questions")
        return 0
    if a[:1] == ["--eval"]:
        return evaluate(cfg, int(a[1]) if len(a) > 1 else 100)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
