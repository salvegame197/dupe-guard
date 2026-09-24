#!/usr/bin/env python3
"""
csearch — symbol index for the current repository.

Answers "does this already exist here?" before new code is written. The index
is ALWAYS DERIVED FROM DISK; not one line of it is hand written. A note can
age into a lie, an index rebuilt on every query cannot.

Usage:
    csearch.py <terms>               search the repo of the current directory
    csearch.py --json <terms>        machine-readable output
    csearch.py --stats               index size
    csearch.py --rebuild             force a full rebuild
    csearch.py --repo <path>         point at another repo

Incremental: only reads files whose mtime changed since the last pass.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

CACHE_DIR = Path(os.environ.get("QUALIDADE_GUARD_HOME")
                 or (Path.home() / ".qualidade-guard")) / "cache"

# Bump when the index format changes: a stale cache is discarded rather than
# misread. An index that lies silently is worse than no index.
INDEX_V = 2

SKIP_DIRS = {
    "node_modules", ".git", "dist", "build", ".next", "out", "vendor",
    "__pycache__", "target", ".venv", "venv", "coverage", ".turbo",
    ".idea", ".vscode", "storage", "bootstrap", "public", "assets",
    "ios", "android", ".expo", "migrations", ".cache", "tmp", "logs",
    ".pytest_cache", "bower_components", "Pods", ".gradle", "bin", "obj",
}

EXTS = {
    ".ts": "ts", ".tsx": "ts", ".js": "ts", ".jsx": "ts", ".mjs": "ts",
    ".cjs": "ts", ".vue": "ts", ".svelte": "ts",
    ".php": "php", ".py": "py", ".rs": "rs", ".go": "go",
}

SKIP_FILE = re.compile(
    r"(\.min\.|\.d\.ts$|\.test\.|\.spec\.|\.stories\.|-lock\.|\.config\.)"
)

MAX_BYTES = 400_000

# -------------------------------------------------------------- extraction

# Each pattern yields (kind, name). These are heuristics, not parsers: high
# recall, cheap to maintain. A missed symbol costs one suggestion.
PATTERNS = {
    "ts": [
        ("fn",    re.compile(r"^\s*export\s+(?:async\s+)?function\s+(\w+)")),
        ("fn",    re.compile(r"^\s*export\s+const\s+(\w+)\s*[:=]\s*(?:async\s*)?[\(<]")),
        ("const", re.compile(r"^\s*export\s+const\s+(\w+)\s*[:=]")),
        ("class", re.compile(r"^\s*export\s+(?:default\s+)?(?:abstract\s+)?class\s+(\w+)")),
        ("type",  re.compile(r"^\s*export\s+(?:interface|type|enum)\s+(\w+)")),
        ("fn",    re.compile(r"^(?:async\s+)?function\s+(\w+)")),
        ("fn",    re.compile(r"^const\s+(\w+)\s*[:=]\s*(?:async\s*)?\(")),
        ("class", re.compile(r"^(?:abstract\s+)?class\s+(\w+)")),
        ("route", re.compile(r"(?:router|app)\.(?:get|post|put|patch|delete|use)\(\s*['\"`]([^'\"`]+)")),
    ],
    "php": [
        ("class", re.compile(r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)")),
        ("class", re.compile(r"^\s*(?:interface|trait)\s+(\w+)")),
        ("fn",    re.compile(r"^\s*(?:public|protected|private)?\s*(?:static\s+)?function\s+(\w+)\s*\(")),
        ("route", re.compile(r"Route::(?:get|post|put|patch|delete|apiResource|resource)\(\s*['\"]([^'\"]+)")),
    ],
    "py": [
        ("fn",    re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")),
        ("class", re.compile(r"^\s*class\s+(\w+)")),
    ],
    "rs": [
        ("fn",    re.compile(r"^\s*pub(?:\([^)]*\))?\s+(?:async\s+)?fn\s+(\w+)")),
        ("fn",    re.compile(r"^\s*(?:async\s+)?fn\s+(\w+)")),
        ("class", re.compile(r"^\s*pub(?:\([^)]*\))?\s+(?:struct|enum|trait)\s+(\w+)")),
    ],
    "go": [
        ("fn",    re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)")),
        ("class", re.compile(r"^type\s+(\w+)\s+(?:struct|interface)")),
    ],
}

PRIVATE_PY = re.compile(r"^_")


def extract(path: Path, lang: str):
    """Read a file and return its top-level symbols."""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []
    return extract_text(text, lang)


def extract_text(text: str, lang: str):
    """Same extraction, over in-memory text (code not yet written)."""
    out, seen = [], set()
    for i, line in enumerate(text.splitlines(), 1):
        if len(line) > 400:
            continue
        # Scan ALL patterns on the line, not just the first. Stopping at the
        # first loses symbols in compact code ('class X { public function y()'
        # on one line) and in route files, where several routes share a line.
        # line_names dedupes a name matching two patterns at once.
        line_names = set()
        for kind, rx in PATTERNS.get(lang, []):
            for m in rx.finditer(line):
                name = m.group(1)
                if not name or len(name) < 3:
                    continue
                if lang == "py" and PRIVATE_PY.match(name):
                    continue
                if name in line_names:
                    continue
                line_names.add(name)
                key = (kind, name)
                if key in seen:
                    continue
                seen.add(key)
                out.append({
                    "n": name,
                    "k": kind,
                    "l": i,
                    "s": line.strip()[:140],
                })
    return out


# ------------------------------------------------------------------ cleanup

STR_PATTERNS = [
    re.compile(r'"(?:\\.|[^"\\])*"'),
    re.compile(r"'(?:\\.|[^'\\])*'"),
    re.compile(r"`(?:\\.|[^`\\])*`"),
]
LINE_COMMENT = {
    "ts": re.compile(r"//.*$"), "rs": re.compile(r"//.*$"),
    "go": re.compile(r"//.*$"),
    "php": re.compile(r"(//|#(?!\[)).*$"), "py": re.compile(r"#.*$"),
}


def strip_noise(line: str, lang: str) -> str:
    """Strip strings and comments: otherwise a '{' inside text breaks the count."""
    for rx in STR_PATTERNS:
        line = rx.sub('""', line)
    rx = LINE_COMMENT.get(lang)
    return rx.sub("", line) if rx else line


def clean_lines(text: str, lang: str):
    out, in_block = [], False
    for raw in text.splitlines():
        line = raw
        if lang != "py":
            if in_block:
                if "*/" in line:
                    line = line.split("*/", 1)[1]
                    in_block = False
                else:
                    out.append("")
                    continue
            line = re.sub(r"/\*.*?\*/", "", line)
            if "/*" in line:
                line = line.split("/*", 1)[0]
                in_block = True
        out.append(strip_noise(line, lang))
    return out


# ---------------------------------------------------------------- functions

DECL = {
    "ts": re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*"
                     r"(?:function\s+(\w+)|(\w+)\s*[:=]\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*(?::[^=]+)?=>)"
                     r"|(\w+)\s*\([^)]*\)\s*(?::\s*[\w<>\[\]|\s,]+)?\s*\{)"),
    "php": re.compile(r"^\s*(?:public|protected|private)?\s*(?:static\s+)?function\s+(\w+)\s*\("),
    "rs":  re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"),
    "go":  re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"),
    "py":  re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)"),
}

# Block openers that are not functions - without this, 'if (x) {' is a function.
NAO_FUNCAO = {"if", "for", "while", "switch", "catch", "do", "else", "foreach",
              "return", "match", "case", "try", "function", "fn", "def", "new"}


def functions(text: str, lang: str):
    """
    [{name, start, end, body, raw}] — body delimited by braces or indentation.

    `body` has strings and comments removed (to count braces and tokens);
    `raw` is the original text, index for index. Showing `body` to the user
    would print `readFileSync("", "")` — mangled evidence is not evidence.
    """
    lines = clean_lines(text, lang)
    orig = text.splitlines()
    rx = DECL.get(lang)
    if not rx:
        return []
    out = []

    if lang == "py":
        for i, line in enumerate(lines):
            m = rx.match(line)
            if not m:
                continue
            indent, name = len(m.group(1)), m.group(2)
            end = len(lines)
            for j in range(i + 1, len(lines)):
                s = lines[j]
                if s.strip() and (len(s) - len(s.lstrip())) <= indent:
                    end = j
                    break
            out.append({"name": name, "start": i + 1, "end": end,
                        "body": lines[i:end], "raw": orig[i:end]})
        return out

    for i, line in enumerate(lines):
        m = rx.match(line)
        if not m:
            continue
        name = next((g for g in m.groups() if g), None)
        if not name or name in NAO_FUNCAO:
            continue
        # find the opening brace (it may be on the next line)
        depth, start_j, opened = 0, None, False
        for j in range(i, min(i + 4, len(lines))):
            if "{" in lines[j]:
                start_j = j
                break
        if start_j is None:
            continue
        for j in range(start_j, len(lines)):
            for ch in lines[j]:
                if ch == "{":
                    depth += 1
                    opened = True
                elif ch == "}":
                    depth -= 1
            if opened and depth <= 0:
                out.append({"name": name, "start": i + 1, "end": j + 1,
                            "body": lines[i:j + 1], "raw": orig[i:j + 1]})
                break
    return out



# ------------------------------------------------------------ fingerprint

# Identifiers collapse to 'v', so renaming does not hide a clone. This is
# what a name search cannot see: handleCreditError, handleDebitError and
# handlePixError are one body under three names.
FP_TOK = re.compile(r"[A-Za-z_]\w*|[{}()\[\];,.]|[-+*/%=<>!&|]+|\d+")
FP_KEEP = {
    "if", "else", "elif", "for", "foreach", "while", "return", "function",
    "new", "try", "catch", "finally", "switch", "case", "break", "continue",
    "public", "private", "protected", "static", "const", "let", "var",
    "await", "async", "throw", "def", "class", "fn", "pub", "match", "use",
}
FP_MIN = 60          # abaixo disso, bloco coincide por acaso (getter, CRUD raso)


def fingerprint(body):
    toks = [t if (t in FP_KEEP or not re.match(r"^[A-Za-z_]\w*$", t)) else "v"
            for l in body for t in FP_TOK.findall(l)]
    if len(toks) < FP_MIN:
        return None, len(toks)
    return hashlib.sha1(" ".join(toks).encode()).hexdigest()[:16], len(toks)


def fingerprints(text: str, lang: str):
    out = []
    for f in functions(text, lang):
        fp, n = fingerprint(f["body"])
        if fp:
            out.append({"n": f["name"], "l": f["start"], "fp": fp, "t": n})
    return out


# ----------------------------------------------------------------- index

def repo_root(start: Path) -> Path:
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(start), capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip())
    except Exception:
        pass
    return start


def cache_path(root: Path) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", root.name).strip("-") or "repo"
    h = hashlib.sha1(str(root).encode()).hexdigest()[:8]
    return CACHE_DIR / f"{slug}-{h}.json"


def walk(root: Path):
    """Every code file in the repo, skipping junk directories."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            name = e.name
            if name.startswith(".") and name not in {".claude"}:
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if name not in SKIP_DIRS:
                        stack.append(Path(e.path))
                    continue
                ext = os.path.splitext(name)[1]
                if ext not in EXTS or SKIP_FILE.search(name):
                    continue
                st = e.stat()
                if st.st_size > MAX_BYTES:
                    continue
                yield Path(e.path), EXTS[ext], int(st.st_mtime)
            except OSError:
                continue


def build(root: Path, force: bool = False, budget: float = 0.0) -> dict:
    """Rebuild the index, re-reading only what changed."""
    cp = cache_path(root)
    old = {}
    if cp.exists() and not force:
        try:
            prev = json.loads(cp.read_text())
            old = prev.get("files", {}) if prev.get("v") == INDEX_V else {}
        except Exception:
            old = {}

    files, reused, t0 = {}, 0, time.time()
    for path, lang, mtime in walk(root):
        rel = str(path.relative_to(root))
        prev = old.get(rel)
        if prev and prev.get("m") == mtime:
            files[rel] = prev
            reused += 1
            continue
        if budget and (time.time() - t0) > budget:
            # Over budget: save what we have and leave. The next turn resumes
            # from here. A partial index now beats a complete one that stalled
            # the user's edit.
            if prev:
                files[rel] = prev
            continue
        try:
            texto = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            texto = ""
        files[rel] = {"m": mtime, "g": lang, "y": extract_text(texto, lang),
                      "p": fingerprints(texto, lang)}

    data = {
        "v": INDEX_V,
        "root": str(root),
        "built": int(time.time()),
        "files": files,
        "nfiles": len(files),
        "nsyms": sum(len(f.get("y", [])) for f in files.values()),
        "reused": reused,
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(cp)
    except Exception:
        pass
    return data


def load(root: Path, max_age: int = 90, budget: float = 0.0) -> dict:
    """Fresh enough. Under max_age, use the cache without revalidating."""
    cp = cache_path(root)
    if cp.exists():
        try:
            data = json.loads(cp.read_text())
            if (data.get("v") == INDEX_V
                    and (time.time() - data.get("built", 0)) < max_age):
                return data
        except Exception:
            pass
    return build(root, budget=budget)


# --------------------------------------------------------------- search

def fold(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


SPLIT = re.compile(r"[^a-z0-9]+")
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def tokens(s: str):
    """createUserCart -> {create, user, cart}. Useful matching is piecewise."""
    s = CAMEL.sub(" ", s)
    return {t for t in SPLIT.split(fold(s)) if len(t) > 2}


# Naming noise: matches half the repo and proves no relationship.
GENERIC = {
    "get", "set", "new", "run", "use", "the", "and", "for", "all", "any",
    "create", "update", "delete", "remove", "find", "fetch", "load", "save",
    "make", "build", "send", "check", "validate", "parse", "format", "render",
    "add", "insert", "select", "process", "execute", "apply", "store", "show",
    "data", "item", "items", "list", "value", "index", "main", "init",
    "handler", "handle", "type", "types", "util", "utils", "helper", "helpers",
    "common", "base", "core", "app", "src", "lib", "api", "service", "services",
    "controller", "model", "component", "components", "page", "pages", "test",
}


def search(data: dict, query: str, limit: int = 8, exclude: str = ""):
    qt = tokens(query)

    # Single short word ('status', 'index', 'store'): in a framework that is a
    # convention slot, not a concept. Every Laravel controller has a 'status';
    # matching on it returns the whole project. A long single word still counts.
    if len(qt) == 1 and max((len(t) for t in qt), default=0) < 8:
        return []

    strong = qt - GENERIC
    if not strong:
        strong = qt
    if not strong:
        return []

    results = []
    for rel, f in data.get("files", {}).items():
        if exclude and rel == exclude:
            continue
        ptok = tokens(rel)
        for sym in f.get("y", []):
            name = sym["n"]
            ntok = tokens(name)
            if not ntok:
                continue

            hit = strong & ntok
            if not hit:
                # with no name match, the path alone is not enough
                continue

            # A single shared token is almost never proof: 'createOrder' and
            # 'createInvoice' share one and are unrelated. Require two signals -
            # two tokens, or one name contained in the other, or the same name.
            # This is the filter that separates a finding from noise.
            contained = ((len(ntok) >= 2 and ntok <= qt) or
                         (len(qt) >= 2 and qt <= ntok))
            if not (len(hit) >= 2 or contained
                    or fold(name) == fold(query.strip())):
                continue

            # Coverage both ways: how much of the query the symbol covers, and
            # how much of the symbol the query explains. The first alone would
            # match 'createOrderWithPaymentAndInvoice' to 'createOrder'.
            score = 4.0 * (len(hit) / len(strong)) + 2.0 * (len(hit) / len(ntok))

            # Containment: the existing name fits whole inside the new one
            # ('formatCurrency' inside 'formatCurrencyBRL'). Strongest signal of
            # reimplementation there is - and the one coverage alone PENALISES,
            # since a longer new name dilutes the fraction. Without this bonus,
            # the more specific the invented name, the quieter the index.
            if len(ntok) >= 2 and ntok <= qt:
                score += 4.0
            elif len(qt) >= 2 and qt <= ntok:
                score += 3.0            # o inverso: escrevo o geral, existe o especifico

            if fold(name) == fold(query.strip()):
                score += 6.0            # mesmo nome: e o caso que importa
            if strong & ptok:
                score += 1.0            # caminho tambem fala do assunto
            if sym["k"] in ("fn", "class"):
                score += 0.5
            if "resources/views/" in rel or "/examples/" in rel:
                score -= 2.0    # a template is not reusable code
            if len(hit) >= 2:
                score += 1.0

            results.append({
                "name": name, "kind": sym["k"], "path": rel,
                "line": sym["l"], "sig": sym["s"], "score": round(score, 2),
            })

    results.sort(key=lambda r: (-r["score"], r["path"]))
    return results[:limit]


# ---------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--repo")
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--exclude", default="")
    a = ap.parse_args()

    root = repo_root(Path(a.repo).resolve() if a.repo else Path.cwd())

    if a.rebuild:
        d = build(root, force=True)
        print(f"{root.name}: {d['nfiles']} files, {d['nsyms']} symbols")
        return 0

    data = load(root)

    if a.stats or not a.query:
        age = int(time.time() - data.get("built", 0))
        print(f"repo  : {data.get('root')}")
        print(f"index : {data.get('nfiles', 0)} files, "
              f"{data.get('nsyms', 0)} symbols ({age}s ago)")
        return 0

    hits = search(data, " ".join(a.query), a.limit, a.exclude)

    if a.json:
        print(json.dumps({"root": str(root), "hits": hits}))
        return 0

    if not hits:
        print("nothing similar in the index.")
        return 0

    for h in hits:
        print(f"{h['score']:6.1f}  {h['kind']:<5} {h['name']}")
        print(f"        {h['path']}:{h['line']}")
        print(f"        {h['sig']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
