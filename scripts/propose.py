#!/usr/bin/env python3
"""
propose — reads the repo and PROPOSES conventions, with the counts as proof.

Exists because `conventions/<repo>.md` starts empty and, if it depends on
someone remembering to dictate rules, stays empty forever.

Writes nothing. It prints a draft for a human to approve, reject or correct.
A rule the model writes on its own becomes permanent law without ever having
been decided.

Each category reads one of two ways:
  - one style dominates -> rule candidate ("here it is always done this way")
  - several are tied    -> the divergence IS the finding ("three ways to do the
                           same thing; picking one is worth more than any rule")

Usage:
    propose.py                 repo of the current directory
    propose.py --repo <path>
"""

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import csearch                                              # noqa: E402

DOMINANT = 0.70     # above this it is the pattern; below, a divergence

CONV_DIR = Path(os.environ.get("DUPE_GUARD_HOME")
                or (Path.home() / ".dupe-guard")) / "conventions"

CATEGORIES = [
    ("Error handling", {
        "custom exception": r"throw new (?!Error\b|Exception\b)\w+",
        "bare Error/Exception": r"throw new (?:Error|Exception|\\Exception)\b",
        "Laravel abort()": r"\babort\(",
        "error returned without throw": r"return\s+(?:\[\s*['\"]error|\{\s*error)",
    }),
    ("API response", {
        "response()->json": r"response\(\)->json\(",
        "custom helper": r"->(?:sendResponse|sendError|successResponse|errorResponse)\(",
        "res.json (express)": r"\bres\.(?:json|status)\(",
        "NextResponse": r"NextResponse\.(?:json|next)\(",
    }),
    ("Input validation", {
        "FormRequest": r"extends\s+FormRequest",
        "inline validate()": r"->validate\(|Validator::make\(",
        "zod": r"\bz\.(?:object|string|number)\(",
        "joi/yup": r"\b(?:Joi|yup)\.\w+\(",
    }),
    ("Date and time", {
        "Carbon": r"\bCarbon::",
        "dayjs": r"\bdayjs\(",
        "moment": r"\bmoment\(",
        "date-fns": r"from ['\"]date-fns",
        "native Date": r"new Date\(",
    }),
    ("HTTP client", {
        "axios": r"\baxios\.",
        "fetch": r"\bfetch\(",
        "Guzzle": r"\bGuzzleHttp\\|new Client\(",
        "Laravel Http::": r"\bHttp::",
    }),
    ("Logging", {
        "Laravel Log::": r"\bLog::",
        "console": r"\bconsole\.(?:log|error|warn)\(",
        "custom logger": r"\blogger\.\w+\(",
        "winston/pino": r"\b(?:winston|pino)\b",
    }),
]

RIVAL_LIBS = [
    ({"moment", "dayjs", "date-fns", "luxon"}, "date library"),
    ({"axios", "node-fetch", "got", "superagent"}, "HTTP client"),
    ({"lodash", "ramda", "underscore"}, "utilities"),
    ({"yup", "joi", "zod", "class-validator"}, "validation"),
]


def count(root: Path, data: dict):
    texts = []
    for rel in data.get("files", {}):
        try:
            texts.append((rel, (root / rel).read_text(encoding="utf-8",
                                                      errors="ignore")))
        except Exception:
            continue
    everything = "\n".join(t for _, t in texts)

    result = []
    for name, options in CATEGORIES:
        c = Counter({k: len(re.findall(v, everything)) for k, v in options.items()})
        c = Counter({k: v for k, v in c.items() if v})
        if c:
            result.append((name, c))
    return result, texts


def rival_libs(root: Path):
    deps = set()
    for f, keys in [("package.json", ("dependencies", "devDependencies")),
                    ("composer.json", ("require", "require-dev"))]:
        p = root / f
        if not p.exists():
            continue
        try:
            j = json.loads(p.read_text())
        except Exception:
            continue
        for k in keys:
            deps |= {d.split("/")[-1] for d in (j.get(k) or {})}
    return [(sorted(group & deps), role)
            for group, role in RIVAL_LIBS if len(group & deps) > 1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo")
    a = ap.parse_args()

    root = csearch.repo_root(Path(a.repo).resolve() if a.repo else Path.cwd())
    data = csearch.load(root)
    result, _ = count(root, data)
    rivals = rival_libs(root)

    print(f"# Proposed conventions — {root.name}")
    print(f"\n> DRAFT, not written to disk. Confirm, correct or reject "
          f"each line.\n> Based on {data.get('nfiles', 0)} files in the repo.\n")

    rules, divergences = [], []
    for name, c in result:
        total = sum(c.values())
        top, n = c.most_common(1)[0]
        share = n / total
        detail = ", ".join(f"{k} {v}x" for k, v in c.most_common(4))
        if share >= DOMINANT:
            rules.append((name, top, share, detail))
        else:
            divergences.append((name, detail))

    if rules:
        print("## Dominant patterns (rule candidates)\n")
        for name, top, share, detail in rules:
            print(f"- **{name}**: `{top}` — {int(share * 100)}% of uses "
                  f"({detail})")
        print()

    if divergences:
        print("## Divergences (the choice has not been made yet)\n")
        print("There is no rule to write here — there is a decision to make. "
              "Two styles living together cost more than either one.\n")
        for name, detail in divergences:
            print(f"- **{name}**: {detail}")
        print()

    if rivals:
        print("## Rival libraries installed\n")
        for libs, role in rivals:
            print(f"- {role}: {', '.join(libs)} — the project pays for "
                  f"{len(libs)} and needs one")
        print()

    print("---")
    print("To become a convention, each line needs a **file path** that proves "
          f"it.\nWithout a path it does not go into `{CONV_DIR / (root.name + '.md')}`.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
