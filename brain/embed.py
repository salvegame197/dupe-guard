#!/usr/bin/env python3
"""
embed — meaning, not words: local embeddings for the search index.

Keyword search is the base and stays the base: on the vault this was built
on it reaches 70% of expected pages on known-answer questions. What it cannot
reach is paraphrase: "relogio" for a page titled "Apple Watch", "celular" for
"iPhone". An embedding puts those next to each other.

Optional, off by default, and local: vectors come from an Ollama server on
this machine (default model bge-m3, multilingual), nothing leaves it, no key.
When the server is down, search silently falls back to keywords. Only the
standard library is required; numpy is used for the dot products when it is
installed and a plain loop otherwise.

Units are the index's turns (a vault page's sections, a session's turns),
embedded once and kept by content hash, so a rebuild only encodes what
changed.

Usage:
    embed.py --build      encode new or changed units
    embed.py --stats
    embed.py <text>       nearest units (for a look)
"""
import hashlib
import json
import math
import sys
import time
import urllib.error
import urllib.request
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, load_config                                   # noqa: E402

VEC = DATA / "embeddings.bin"
META = DATA / "embeddings.json"
BATCH = 32
MAX_CHARS = 1500            # bge-m3 handles more, but a section's head says what it is about
MAX_UNITS_PER_DOC = 40


def settings(cfg):
    e = dict({"enabled": False, "url": "http://localhost:11434", "model": "bge-m3",
              "weight": 30.0, "min_cos": 0.45, "strong_cos": 0.6, "top": 20}, **(cfg.get("embeddings") or {}))
    return e


def embed_texts(texts, cfg, timeout=120):
    """[[float]] from the local server, or None when it is not there."""
    e = settings(cfg)
    req = urllib.request.Request(f"{e['url'].rstrip('/')}/api/embed",
                                 data=json.dumps({"model": e["model"], "input": texts}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            out = json.loads(r.read())["embeddings"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None
    return [normalise(v) for v in out]


def normalise(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def units_from_index():
    """(unit_id, text) for everything worth embedding."""
    from brain import ksearch
    for doc in ksearch.load_index():
        turns = doc["turns"][:MAX_UNITS_PER_DOC]
        for i, t in enumerate(turns):
            text = t["t"][:MAX_CHARS]
            if len(text) < 40:
                continue
            # the title is part of the meaning of a section
            yield f"{doc['id']}#{i}", f"{doc['title']}\n{text}"


def load_meta():
    try:
        return json.loads(META.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"dims": 0, "ids": [], "hashes": []}


def load_vectors(meta):
    if not VEC.exists() or not meta["dims"]:
        return None
    try:
        import numpy as np
        return np.fromfile(VEC, dtype="float32").reshape(-1, meta["dims"])
    except ImportError:
        a = array("f")
        with VEC.open("rb") as f:
            a.frombytes(f.read())
        return a


def build(cfg) -> int:
    meta = load_meta()
    known = dict(zip(meta["ids"], meta["hashes"]))
    old_vecs = load_vectors(meta)
    keep_ids, keep_hashes, new_ids, new_texts, new_hashes = [], [], [], [], []
    kept_rows = []
    pos = {uid: i for i, uid in enumerate(meta["ids"])}
    for uid, text in units_from_index():
        h = hashlib.sha1(text.encode()).hexdigest()[:16]
        if known.get(uid) == h and old_vecs is not None:
            keep_ids.append(uid); keep_hashes.append(h); kept_rows.append(pos[uid])
        else:
            new_ids.append(uid); new_texts.append(text); new_hashes.append(h)
    started = time.time()
    vectors = []
    for i in range(0, len(new_texts), BATCH):
        got = embed_texts(new_texts[i:i + BATCH], cfg)
        if got is None:
            print("embedding server not reachable; nothing changed.", file=sys.stderr)
            return 1
        vectors.extend(got)
    dims = len(vectors[0]) if vectors else meta["dims"]
    out = array("f")
    for row in kept_rows:
        out.extend(_row(old_vecs, row, meta["dims"]))
    for v in vectors:
        out.extend(v)
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = VEC.with_suffix(".tmp")
    with tmp.open("wb") as f:
        out.tofile(f)
    tmp.replace(VEC)
    META.write_text(json.dumps({"dims": dims, "ids": keep_ids + new_ids, "hashes": keep_hashes + new_hashes,
                                "model": settings(cfg)["model"], "built": int(time.time())}), encoding="utf-8")
    print(f"embedded {len(new_ids)} new unit(s), kept {len(keep_ids)}, {dims} dims, "
          f"{time.time() - started:.1f}s")
    return 0


def _row(vecs, i, dims):
    try:
        return vecs[i].tolist()
    except AttributeError:
        return vecs[i * dims:(i + 1) * dims]


def search(text, cfg, top=None):
    """[(doc_id, cos)] best document per query, max over its units."""
    e = settings(cfg)
    meta = load_meta()
    vecs = load_vectors(meta)
    if vecs is None or not meta["ids"]:
        return []
    q = embed_texts([text[:MAX_CHARS]], cfg, timeout=20)
    if not q:
        return []
    q = q[0]
    try:
        import numpy as np
        sims = vecs @ np.asarray(q, dtype="float32")
        order = np.argsort(-sims)[: (top or e["top"]) * 4]
        scored = [(int(i), float(sims[i])) for i in order]
    except ImportError:
        dims = meta["dims"]
        scored = []
        for i in range(len(meta["ids"])):
            base = i * dims
            s = 0.0
            for k in range(dims):
                s += vecs[base + k] * q[k]
            scored.append((i, s))
        scored.sort(key=lambda x: -x[1])
        scored = scored[: (top or e["top"]) * 4]
    best = {}
    for i, s in scored:
        doc = meta["ids"][i].rsplit("#", 1)[0]
        if s >= e["min_cos"] and (doc not in best or s > best[doc]):
            best[doc] = s
    return sorted(best.items(), key=lambda x: -x[1])[: top or e["top"]]


def main() -> int:
    cfg = load_config()
    a = sys.argv[1:]
    if a[:1] == ["--build"]:
        return build(cfg)
    if a[:1] == ["--stats"]:
        m = load_meta()
        print(f"{len(m['ids'])} units, {m['dims']} dims, model {m.get('model')}")
        return 0
    if a:
        for doc, s in search(" ".join(a), cfg):
            print(f"  {s:.3f}  {doc}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
