#!/usr/bin/env python3
"""
truth — score recall against questions whose answers are known.

Measuring whether the model USES what is injected (hit_rate) is not the same
as measuring whether what was injected was RIGHT. This is the second half: a
page in the vault holds questions the user can answer, each with the page
that should come back. Every change to recall is judged here, before and
after.

The page is a markdown table with at least these columns, in any order:
`pergunta`/`question`, `página esperada`/`expected page`. Several expected
pages may be separated by ` / `. Rows are matched by their first cell being
a number.

Three outcomes, kept apart on purpose:
  hit      an expected page is among the injected ones
  miss     recall injected, but none of them
  silent   recall injected nothing
A change can trade one failure for the other; the score must show both.

Usage:
    truth.py                 run and print
    truth.py --save          also keep the result in the data folder, for diffs
    truth.py --diff          compare the last two saved results
"""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, fold, load_config                             # noqa: E402

RESULTS = DATA / "truth"
HEADER_Q = ("pergunta", "question")
HEADER_P = ("página esperada", "pagina esperada", "expected page")


def load_questions(page: Path):
    rows, cols = [], None
    for line in page.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        low = [fold(c) for c in cells]
        if any(h in low for h in HEADER_Q):
            cols = {"q": next(i for i, c in enumerate(low) if c in HEADER_Q),
                    "p": next(i for i, c in enumerate(low) if c in HEADER_P)}
            continue
        if cols and cells and re.fullmatch(r"\d+", cells[0]) and len(cells) > max(cols.values()):
            expected = [e.strip() for e in re.split(r" / |;", cells[cols["p"]]) if e.strip()]
            rows.append({"n": int(cells[0]), "question": cells[cols["q"]], "expected": expected})
    return rows


def matches(expected: str, title: str) -> bool:
    e, t = fold(expected), fold(title)
    return e == t or (len(e) >= 12 and (e in t or t in e))


def run(cfg, page=None):
    from brain import recall
    out = []
    for q in load_questions(cfg["vault"] / (page or cfg["truth_page"])):
        terms, matched, picked = recall.recall(q["question"], "", cfg)
        titles = [p["title"] for p in picked]
        if not picked:
            status = "silent"
        elif any(matches(e, t) for e in q["expected"] for t in titles):
            status = "hit"
        else:
            status = "miss"
        out.append({**q, "status": status, "terms": terms, "got": titles})
    return out


def summary(rows):
    n = len(rows) or 1
    c = {s: sum(r["status"] == s for r in rows) for s in ("hit", "miss", "silent")}
    return {**c, "total": len(rows), "hit_pct": round(100 * c["hit"] / n, 1)}


def main() -> int:
    cfg = load_config()
    if "--diff" in sys.argv:
        files = sorted(RESULTS.glob("*.json"))[-2:]
        if len(files) < 2:
            print("need two saved runs to diff")
            return 1
        a, b = (json.loads(f.read_text()) for f in files)
        sa, sb = summary(a["rows"]), summary(b["rows"])
        print(f"{files[0].stem} -> {files[1].stem}")
        for k in ("hit", "miss", "silent"):
            print(f"  {k:<7}{sa[k]:>4} -> {sb[k]:<4}")
        byn = {r["n"]: r for r in a["rows"]}
        for r in b["rows"]:
            before = byn.get(r["n"], {}).get("status")
            if before and before != r["status"]:
                print(f"  #{r['n']:<3} {before} -> {r['status']}   {r['question'][:60]}")
        return 0

    # --page "Other.md": a second question set, kept blind (never tuned on).
    page = sys.argv[sys.argv.index("--page") + 1] if "--page" in sys.argv else None
    rows = run(cfg, page)
    if not rows:
        print(f"no questions found in {cfg['vault'] / (page or cfg['truth_page'])}")
        return 1
    s = summary(rows)
    for r in rows:
        mark = {"hit": "✔", "miss": "✘", "silent": "·"}[r["status"]]
        got = "; ".join(t[:38] for t in r["got"][:2]) if r["got"] else "—"
        print(f" {mark} #{r['n']:<3} {r['question'][:58]:<58} {got}")
    print(f"\nhit {s['hit']}  miss {s['miss']}  silent {s['silent']}  of {s['total']}  ({s['hit_pct']}% hit)")
    if "--save" in sys.argv:
        RESULTS.mkdir(parents=True, exist_ok=True)
        f = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
        f.write_text(json.dumps({"summary": s, "rows": rows}, ensure_ascii=False, indent=1))
        print(f"saved: {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
