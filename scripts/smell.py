#!/usr/bin/env python3
"""
smell — structural problems the eye misses and a machine finds.

Not a substitute for judgement: it points at WHERE to look, with the concrete
reason attached. A bare number ("CC=14") is a report nobody reads; a number
with the why ("14 paths, 4 levels of nested if, the happy path is at the
bottom") is an address.

Detects:
  complexity     decision paths per function (cyclomatic)
  nesting        depth; buried happy path (missing guard clause)
  n+1            database query inside a loop
  sequential     await inside a loop (serialised I/O that could be parallel)
  sync-io        readFileSync and friends on the request path
  cycle          circular import between files
  layer          inverted import (model -> controller, controller -> controller)

Usage:
    smell.py                    files changed in the working diff
    smell.py --base main        everything the branch changed
    smell.py --all              the whole repo (slow; for diagnosis)
    smell.py --file <path>      one file
    smell.py --orphans          declared symbols nobody uses (scans the repo)
    smell.py --json
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import csearch                                              # noqa: E402

# Thresholds. Not law: the point where a human should look.
CC_WARN = 11            # the classic; below it there is rarely anything to say
CC_SEVERE = 20
NEST_WARN = 4           # 4 levels inside a function: the happy path is gone
GUARD_MIN_LINES = 12    # a short function wrapped in an if bothers nobody

# The function-body machinery lives in csearch (the extraction layer).
from csearch import functions                                # noqa: E402

# ----------------------------------------------------------------- measures

CC_TOKENS = re.compile(
    r"\b(if|elif|else\s+if|elseif|for|foreach|while|case|catch|rescue|when)\b"
    r"|&&|\|\||\?\?|(?<![:?])\?(?!\?)"
)


def complexity(body) -> int:
    return 1 + sum(len(CC_TOKENS.findall(l)) for l in body)


def nesting(body, lang) -> int:
    """Maximum depth inside the function."""
    if lang == "py":
        base = None
        mx = 0
        for l in body[1:]:
            if not l.strip():
                continue
            ind = len(l) - len(l.lstrip())
            if base is None:
                base = ind
            mx = max(mx, (ind - base) // 4)
        return mx + 1 if base is not None else 0
    depth = mx = 0
    for l in body:
        for ch in l:
            if ch == "{":
                depth += 1
                mx = max(mx, depth)
            elif ch == "}":
                depth -= 1
    return max(mx - 1, 0)


OPENS_IF = re.compile(r"^\s*if\s*[\(:]")
HAS_EXIT = re.compile(r"^\s*(return|throw|raise)\b")


def buried_happy_path(f, lang):
    """
    The whole body inside an opening 'if', with no early exit: the arrow
    anti-pattern. The fix is to invert the condition and leave early.
    """
    body = f["body"]
    if len(body) < GUARD_MIN_LINES:
        return None
    inner = body[1:-1] if lang != "py" else body[1:]
    first = next((k for k, l in enumerate(inner) if l.strip()), None)
    if first is None or not OPENS_IF.match(inner[first]):
        return None
    # already uses a guard clause? then the opening if does not wrap anything
    if any(HAS_EXIT.match(l) for l in inner[first:first + 4]):
        return None
    # does the if cover almost the whole function?
    if lang == "py":
        base = len(inner[first]) - len(inner[first].lstrip())
        end = next((k for k in range(first + 1, len(inner))
                    if inner[k].strip()
                    and len(inner[k]) - len(inner[k].lstrip()) <= base), len(inner))
    else:
        d, end, opened = 0, len(inner), False
        for k in range(first, len(inner)):
            for ch in inner[k]:
                if ch == "{":
                    d += 1; opened = True
                elif ch == "}":
                    d -= 1
            if opened and d <= 0:
                end = k + 1
                break
    coverage = (end - first) / max(len(inner), 1)
    return round(coverage, 2) if coverage >= 0.75 else None


# -------------------------------------------------------------- bottlenecks

LOOP = re.compile(r"\b(for|foreach|while)\s*[\(:]|\.\s*(forEach|map)\s*\(")
QUERY_PHP = re.compile(
    r"->(get|first|find|findOrFail|count|exists|sum|save|update|delete|create|paginate)\s*\("
    r"|::(where|find|findOrFail|create|first|all)\s*\(|\bDB::")
QUERY_TS = re.compile(
    r"\b(prisma|knex|sequelize|db|repository|repo|em|queryRunner)\s*\.\s*\w+"
    r"|\.\s*(query|findOne|findMany|findAll|findByPk|save|insert|update|delete|aggregate)\s*\(")
AWAIT = re.compile(r"\bawait\b")
IO_SYNC = re.compile(r"\b(readFileSync|writeFileSync|appendFileSync|execSync|readdirSync)\b")


def loop_block(body, k, lang):
    """Lines of the loop body that starts at body[k]."""
    if lang == "py":
        base = len(body[k]) - len(body[k].lstrip())
        out = []
        for j in range(k + 1, len(body)):
            if body[j].strip() and (len(body[j]) - len(body[j].lstrip())) <= base:
                break
            out.append((j, body[j]))
        return out
    d, opened, out = 0, False, []
    for j in range(k, len(body)):
        for ch in body[j]:
            if ch == "{":
                d += 1; opened = True
            elif ch == "}":
                d -= 1
        if j > k:
            out.append((j, body[j]))
        if opened and d <= 0:
            break
    return out


EMPTY_CATCH = re.compile(r"catch\s*(?:\([^)]*\))?\s*\{\s*\}"
                         r"|\.catch\(\s*\(\s*\)\s*=>\s*(?:null|undefined|\{\s*\})\s*\)")
LOG_ONLY_CATCH = re.compile(r"catch\s*\([^)]*\)\s*\{\s*(?:console\.\w+|print|echo)\s*\(")
SQL_INTERPOLATED = re.compile(
    r"DB::(?:raw|select|statement|update|delete|insert)\s*\(\s*[\"']?[^\"')]*\$"
    r"|(?:query|execute)\s*\(\s*`[^`]*\$\{")


def traps(f, lang, rel):
    """Swallowed errors and SQL built by interpolation: cheap to find, costly to have."""
    found = []
    raw = f.get("raw") or f["body"]
    for k, line in enumerate(f["body"]):
        snippet = "\n".join(f["body"][k:k + 3])
        if EMPTY_CATCH.search(snippet) or LOG_ONLY_CATCH.search(snippet):
            found.append({
                "kind": "swallowed-error", "line": f["start"] + k,
                "function": f["name"], "fstart": f["start"],
                "detail": "exception caught and discarded — the failure vanishes "
                          "silently and becomes a bug with no trace",
                "weight": 8,
            })
        # SQL must be searched in the ORIGINAL text: the cleaned body replaced
        # every string with "", and the interpolated variable — exactly what
        # we are looking for — went with it.
        original = raw[k] if k < len(raw) else line
        if SQL_INTERPOLATED.search(original):
            txt = original.strip()
            found.append({
                "kind": "sql-interpolation", "line": f["start"] + k,
                "function": f["name"], "fstart": f["start"],
                "detail": f"variable interpolated straight into SQL — use binding: `{txt[:80]}`",
                "weight": 10,
            })
    return found


def bottlenecks(f, lang, rel):
    found = []
    body = f["body"]
    raw = f.get("raw") or body

    def txt(j):
        return (raw[j] if j < len(raw) else body[j]).strip()

    for k, line in enumerate(body):
        if not LOOP.search(line):
            continue
        block = loop_block(body, k, lang)
        if not block:
            continue
        qrx = QUERY_PHP if lang == "php" else QUERY_TS
        queries = [(j, l) for j, l in block if qrx.search(l)]
        if queries:
            j, l = queries[0]
            found.append({
                "kind": "n+1", "line": f["start"] + j, "function": f["name"],
                "fstart": f["start"],
                "detail": f"database query inside a loop ({len(queries)}x): "
                          f"`{txt(j)[:80]}`",
                "weight": 9,
            })
        elif lang != "php":
            aw = [(j, l) for j, l in block if AWAIT.search(l)]
            if aw:
                j, l = aw[0]
                found.append({
                    "kind": "sequential", "line": f["start"] + j,
                    "function": f["name"], "fstart": f["start"],
                    "detail": f"`await` inside a loop ({len(aw)}x): serialised I/O "
                              f"— consider `Promise.all`",
                    "weight": 6,
                })
    for k, line in enumerate(body):
        if IO_SYNC.search(line):
            found.append({
                "kind": "sync-io", "line": f["start"] + k, "function": f["name"],
                "fstart": f["start"],
                "detail": f"synchronous I/O blocks the event loop: `{txt(k)[:70]}`",
                "weight": 7,
            })
    return found


# ------------------------------------------------------------------ imports

IMP_TS = re.compile(r"""(?:from\s+|require\(|import\()\s*['"]([^'"]+)['"]""")
IMP_PHP = re.compile(r"^\s*use\s+(App\\[\w\\]+)(?:\s+as\s+(\w+))?")


def resolve_ts(rel: str, spec: str, files: set):
    if not spec.startswith("."):
        return None
    base = os.path.normpath(os.path.join(os.path.dirname(rel), spec))
    for cand in (base, base + ".ts", base + ".tsx", base + ".js",
                 base + "/index.ts", base + "/index.tsx"):
        if cand in files:
            return cand
    return None


def resolve_php(spec: str, files: set):
    cand = "app/" + spec[4:].replace("\\", "/") + ".php"
    return cand if cand in files else None


INHERITS = re.compile(r"\b(?:extends|implements)\s+([\w, \\]+)")


def graph(root: Path, data: dict):
    """Import graph, plus who inherits from whom (inheritance is not a violation)."""
    files = set(data.get("files", {}).keys())
    g = defaultdict(set)
    parents = defaultdict(set)
    # `use App\Http\Controllers\Api\BaseController as Controller` is common in
    # Laravel: without resolving the alias, `extends Controller` does not match
    # BaseController.php and every inheritance becomes a "layer violation".
    alias = defaultdict(dict)
    for rel, f in data.get("files", {}).items():
        lang = f.get("g")
        try:
            text = (root / rel).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for line in text.splitlines()[:120]:
            h = INHERITS.search(line)
            if h:
                for name in re.split(r"[,\s]+", h.group(1)):
                    name = name.strip().split("\\")[-1]
                    if name:
                        parents[rel].add(name)
            target = None
            if lang == "ts":
                m = IMP_TS.search(line)
                if m:
                    target = resolve_ts(rel, m.group(1), files)
            elif lang == "php":
                m = IMP_PHP.match(line)
                if m:
                    target = resolve_php(m.group(1), files)
                    if m.group(2):
                        alias[rel][m.group(2)] = m.group(1).split("\\")[-1]
            if target and target != rel:
                g[rel].add(target)
    return g, parents, alias


def cycles(g, limit=40):
    """Iterative DFS; returns distinct cycles (normalised by their smallest node)."""
    found, seen = [], set()
    colour = {}

    def dfs(start):
        stack = [(start, iter(sorted(g.get(start, ()))))]
        path = [start]
        colour[start] = 1
        while stack:
            node, it = stack[-1]
            advanced = False
            for nb in it:
                if colour.get(nb) == 1:                 # back edge
                    i = path.index(nb)
                    cycle = path[i:]
                    key = tuple(sorted(cycle))
                    if key not in seen:
                        seen.add(key)
                        found.append(cycle + [nb])
                elif colour.get(nb, 0) == 0:
                    colour[nb] = 1
                    path.append(nb)
                    stack.append((nb, iter(sorted(g.get(nb, ())))))
                    advanced = True
                    break
            if not advanced:
                colour[node] = 2
                stack.pop()
                path.pop()
            if len(found) >= limit:
                return

    for node in sorted(g):
        if colour.get(node, 0) == 0:
            dfs(node)
            if len(found) >= limit:
                break
    return found


# ------------------------------------------------------------------- clones

CLONE_MIN = 100     # tokens: below this, an identical body is coincidence
                    # (getter, shallow CRUD, DTO constructor)


def clones(data: dict, limit: int = 60):
    """
    Functions whose body is identical after every identifier becomes 'v'.

    The gap a name search never covers: `handleCreditError`,
    `handleDebitError` and `handlePixError` are one body under three names,
    and no name query will ever relate them.
    """
    groups = defaultdict(list)
    for rel, f in data.get("files", {}).items():
        for fp in f.get("p", []):
            if fp["t"] >= CLONE_MIN:
                groups[fp["fp"]].append((rel, fp["n"], fp["l"], fp["t"]))
    out = [g for g in groups.values() if len(g) > 1]
    out.sort(key=lambda g: -(g[0][3] * (len(g) - 1)))
    return out[:limit]


LAYER = re.compile(r"(?:^|/)(controllers?|services?|repositor(?:y|ies)|models?|"
                   r"entities|views?|routes?|components?|hooks?|utils?|lib|helpers?)(?:/|$)",
                   re.I)


def layer(rel: str):
    m = LAYER.search(os.path.dirname(rel))
    if not m:
        return None
    c = m.group(1).lower().rstrip("s")
    return {"repositorie": "repository", "entitie": "model", "helper": "util",
            "librarie": "util", "lib": "util"}.get(c, c)


INVERTED = {("model", "controller"), ("model", "service"),
            ("repository", "controller"), ("repository", "service"),
            ("util", "controller"), ("util", "service"), ("util", "model"),
            ("controller", "controller")}


def layer_violations(g, parents, alias):
    """
    Inverted imports, inheritance excluded. Without that, in a Laravel app
    every controller "violates a layer" by extending the base Controller,
    and the real findings get buried under the base class.
    """
    out = []
    for a, neighbours in g.items():
        la = layer(a)
        if not la:
            continue
        al = alias.get(a, {})
        inherited = {al.get(n, n) for n in parents.get(a, ())}
        for b in neighbours:
            if os.path.splitext(os.path.basename(b))[0] in inherited:
                continue
            lb = layer(b)
            if lb and (la, lb) in INVERTED:
                out.append((a, b, la, lb))
    return out


# ------------------------------------------------------------------ orphans

# Entry points called by the framework, never by our own code. Without this
# list every Laravel controller looks dead.
ENTRY = {
    "index", "store", "show", "edit", "update", "destroy", "create",
    "handle", "boot", "register", "up", "down", "rules", "authorize",
    "messages", "attributes", "render", "report", "toArray", "broadcastOn",
    "via", "toMail", "toDatabase", "build", "run", "map", "schedule",
    "main", "setUp", "tearDown", "configure", "provider", "middleware",
}
IDENT = re.compile(r"[A-Za-z_]\w{2,}")


TEST = re.compile(r"(\.test\.|\.spec\.|/__tests__/|(?:^|/)[Tt]ests?/)")


def _all_code_files(root: Path):
    """
    Every code file, tests INCLUDED.

    The index skips tests (nobody reuses a test helper), but for counting
    usage they are evidence: without them a function only tests call looks
    dead, and "appears nowhere" would simply be false.
    """
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            if e.name.startswith("."):
                continue
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in csearch.SKIP_DIRS:
                        stack.append(Path(e.path))
                elif os.path.splitext(e.name)[1] in csearch.EXTS:
                    if e.stat().st_size <= csearch.MAX_BYTES:
                        yield Path(e.path)
            except OSError:
                continue


def orphans(root: Path, data: dict):
    """
    Declared symbols nobody uses — or that only tests use.

    Counting scans the raw text on purpose, so `[Foo::class, 'method']` in a
    route file and `{{ $x->method() }}` in a template count as uses. Calls
    built at runtime (`$obj->{$name}()`) stay invisible, which is why the
    result is a candidate, never a verdict.
    """
    use, use_prod = Counter(), Counter()
    for path in _all_code_files(root):
        try:
            ids = IDENT.findall(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        use.update(ids)
        if not TEST.search(str(path.relative_to(root))):
            use_prod.update(ids)

    out = []
    for rel, f in data.get("files", {}).items():
        if COLD.search(rel) or "/views/" in rel:
            continue
        for sym in f.get("y", []):
            n = sym["n"]
            if (sym["k"] == "route" or n in ENTRY or n.startswith("_")
                    or len(n) < 5 or use_prod[n] > 1):
                continue
            test_only = use[n] > 1
            out.append({
                "kind": "test-only" if test_only else "orphan",
                "file": rel, "line": sym["l"], "function": n, "fstart": sym["l"],
                "detail": (f"`{n}` ({sym['k']}) is only used by tests — the tests "
                           f"keep alive code production never calls"
                           if test_only else
                           f"`{n}` ({sym['k']}) appears nowhere else in the repo "
                           f"— dead code candidate"),
                "weight": 3 if test_only else 4,
            })
    return out


# ---------------------------------------------------------------------- cli

# N+1 in a console command or nightly job: usually acceptable. N+1 in a
# controller is production latency, per request. The same pattern costs
# differently depending on where it runs — without this the report buries
# what matters under maintenance routines.
COLD = re.compile(r"(?:^|/)(Console|Commands?|database|[Mm]igrations?|[Ss]eeders?|"
                  r"[Ff]actories|[Tt]ests?|scripts?|bin)(?:/|$)")
HOT = re.compile(r"(?:^|/)([Cc]ontrollers?|[Rr]outes?|[Mm]iddleware|"
                 r"[Ss]ervices?|[Rr]esolvers?|pages|api)(?:/|$)")


def place_weight(rel: str) -> int:
    if COLD.search(rel):
        return -4
    if HOT.search(rel):
        return 1
    return 0


def git(args, cwd):
    r = subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                       text=True, timeout=60)
    return r.stdout if r.returncode == 0 else ""


def analyse_file(root: Path, rel: str):
    lang = csearch.EXTS.get(os.path.splitext(rel)[1])
    if not lang or csearch.SKIP_FILE.search(os.path.basename(rel)):
        return []
    try:
        text = (root / rel).read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return []

    found = []
    for f in functions(text, lang):
        cc = complexity(f["body"])
        nest = nesting(f["body"], lang)
        guard = buried_happy_path(f, lang)

        if cc >= CC_WARN or nest >= NEST_WARN:
            reasons = []
            if cc >= CC_WARN:
                reasons.append(f"{cc} decision paths")
            if nest >= NEST_WARN:
                reasons.append(f"{nest} levels of nesting")
            found.append({
                "kind": "complexity", "line": f["start"], "function": f["name"],
                "fstart": f["start"],
                "detail": " · ".join(reasons) +
                          (" — severe" if cc >= CC_SEVERE else ""),
                "weight": 8 if cc >= CC_SEVERE else 5 + min(nest, 3),
            })
        if guard:
            found.append({
                "kind": "guard-clause", "line": f["start"], "function": f["name"],
                "fstart": f["start"],
                "detail": f"{int(guard * 100)}% of the body inside an opening `if` "
                          f"— invert the condition and return early",
                "weight": 6,
            })
        found += bottlenecks(f, lang, rel)
        found += traps(f, lang, rel)

    adjust = place_weight(rel)
    for x in found:
        x["file"] = rel
        x["weight"] += adjust
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--orphans", action="store_true",
                    help="look for symbols with no use at all (scans the repo)")
    ap.add_argument("--file")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--repo")
    ap.add_argument("--limit", type=int, default=25)
    a = ap.parse_args()

    root = csearch.repo_root(Path(a.repo).resolve() if a.repo else Path.cwd())
    data = csearch.load(root)

    if a.file:
        targets = [os.path.relpath(os.path.abspath(a.file), root)]
    elif a.all:
        targets = list(data.get("files", {}).keys())
    else:
        diff = (git(["diff", f"{a.base}...HEAD", "--name-only"], root) if a.base
                else git(["diff", "HEAD", "--name-only"], root) or git(["diff", "--name-only"], root))
        targets = [l for l in diff.splitlines() if l.strip()]

    if not targets:
        print("no files to analyse.")
        return 0

    found = []
    for rel in targets:
        found += analyse_file(root, rel)

    # cycles and layers are properties of the graph, not of a file
    g, parents, alias = graph(root, data)
    touched = set(targets)
    for c in cycles(g):
        if touched & set(c) or a.all:
            found.append({
                "kind": "cycle", "file": c[0], "line": 1, "function": "",
                "detail": "circular import: " + " -> ".join(c),
                "weight": 9,
            })
    if a.orphans:
        for o in orphans(root, data):
            found.append(o)

    for group in clones(data):
        files = {r for r, _, _, _ in group}
        if not (touched & files) and not a.all:
            continue
        names = sorted({n for _, n, _, _ in group})
        rel, name, line, ntok = group[0]
        where = "; ".join(f"{r}:{l} ({n})" for r, n, l, _ in group[1:4])
        same_name = len(names) == 1
        found.append({
            "kind": "clone", "file": rel, "line": line, "function": name,
            "fstart": line,
            "detail": (f"identical body ({ntok} tokens) in {len(group)} places"
                       + ("" if same_name else f" under different names ({', '.join(names)})")
                       + f" — also at {where}"),
            # a different name is the case no name search finds: weighs more
            "weight": 10 if not same_name else 8,
        })

    for x, y, lx, ly in layer_violations(g, parents, alias):
        if x in touched or a.all:
            found.append({
                "kind": "layer", "file": x, "line": 1, "function": "",
                "detail": f"{lx} imports {ly}: `{y}`",
                "weight": 6,
            })

    # A nested loop reports the same query once per enclosing loop. Same finding.
    seen, unique = set(), []
    for x in found:
        key = (x["file"], x["kind"], x["line"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(x)

    # One function with 6 queries in loops is ONE problem at 6 points, not six
    # problems. Without collapsing, one bad file drowns the rest of the report.
    groups = {}
    for x in unique:
        k = (x["file"], x["kind"], x["function"], x.get("fstart", x["line"]))
        if k in groups:
            groups[k]["_n"] += 1
            groups[k]["_lines"].append(x["line"])
        else:
            x["_n"], x["_lines"] = 1, [x["line"]]
            groups[k] = x
    found = []
    for x in groups.values():
        if x["_n"] > 1:
            others = ", ".join(str(n) for n in sorted(x["_lines"])[1:6])
            x["detail"] += f" — and at {x['_n'] - 1} more point(s) in the function (lines {others})"
            x["weight"] += min(x["_n"] // 2, 3)
        found.append(x)

    found.sort(key=lambda x: (-x["weight"], x["file"], x["line"]))
    found = found[:a.limit]
    worst = {}
    for x in found:
        worst[x["file"]] = max(worst.get(x["file"], 0), x["weight"])
    found.sort(key=lambda x: (-worst[x["file"]], x["file"],
                              -x["weight"], x["line"]))

    if a.json:
        print(json.dumps({"root": str(root), "findings": found}, indent=2))
        return 0

    if not found:
        print("nothing structural to point at in the analysed files.")
        return 0

    current = None
    for x in found:
        if x["file"] != current:
            current = x["file"]
            print(f"\n## {current}")
        fn = f" in `{x['function']}`" if x["function"] else ""
        print(f"  [{x['kind']}] {current}:{x['line']}{fn}")
        print(f"      {x['detail']}")
    print(f"\n{len(found)} point(s) to look at — the number is an address, not a verdict.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
