---
name: review
description: Review code for duplication (by name and by copied body) and structural problems (complexity, nesting, missing guard clause, N+1 and hidden bottlenecks, swallowed errors, interpolated SQL, circular imports, layer violations, dead code). Use when the user asks to review code, improve a project, find duplication, look for bottlenecks or dead code, or asks whether something already exists in the repository.
---

# review — code that adds to a project instead of piling up

A project does not rot from bad code; it rots from accumulating **lukewarm
code**: three versions of the same function, none trustworthy enough to delete
the others, and a handful of functions nobody understands well enough to touch.

## Principle

**Writing is the cheap part, and the only part a model does fast.** Inventing
a fourth `formatCurrency`, or adding one more `if` to a function that already
has thirty paths, costs the model almost nothing and costs the codebase
permanently. That asymmetry is why this skill exists.

Two consequences:

1. **Duplication is settled before writing.** Once the sibling function
   exists, removing it stops being a technical problem and becomes a social
   one: someone has to admit they rewrote it.
2. **Structure is settled in review**, with the whole code in front of you.
   You cannot judge whether a function is tangled from the line going in.

## The automatic guard (before writing)

The plugin's hook runs on every Write/Edit, and after every Bash command that
changed code, and warns two ways:

- **by name**: about to declare something that already exists;
- **by body**: the block is identical to one that exists under another name.
  That is the half no name search reaches: `handleCreditError`,
  `handleDebitError` and `handlePixError` can be one body under three names.

It never blocks. Three honest ways out:

| Situation | What to do |
|---|---|
| The candidate fits | Import or extend it. Do not write the new version. |
| It fits with a change | Change the existing one. One more case rarely justifies a sibling. |
| It does not fit | Carry on, **and say in one line why it did not fit.** |

That line separates "I checked and it did not fit" from "I ignored it". If you
cannot write it, you did not read the candidate.

## Review mode

### 1. Gather the mechanical evidence

```bash
# what the diff INTRODUCED, and what already existed that looks like it
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/difflook.py            # working diff
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/difflook.py --base main

# structural problems
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/smell.py               # files in the diff
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/smell.py --base main
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/smell.py --all         # whole repo (diagnosis)
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/smell.py --file <path>
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/smell.py --orphans     # unused code (scans the repo)

# "is there already something that does X?"
python3 ${CLAUDE_PLUGIN_ROOT}/csearch.py <terms>
```

**Running the tools is not the review; it is where the review starts.** They
point at addresses. A finding exists only after opening the file and
understanding the case. Reporting tool output without reading the code is
passing on a suspicion as if it were a diagnosis.

### 2. Judge

**Reuse**

1. **Duplication.** A `!` from difflook, confirmed by reading both sides.
2. **Reinventing a dependency.** Before accepting a new helper, check
   `package.json` / `composer.json` / `Cargo.toml`: does the project already
   pay for a library that does this?
3. **Fitting the neighbours.** Compare with the sibling file (same folder,
   same layer). If the new code handles errors, validation or responses in a
   way no sibling does, either the pattern changed and the siblings fell
   behind, or the new code is off-key. Say which.
4. **Premature abstraction.** A layer or interface created for **one** use
   case. Without the second case there is no way to know what varies: the
   abstraction will be wrong and expensive to undo.

**Structure** (`smell.py` points; judgement comes from looking)

5. **Complexity and nesting.** Many decision paths in one function. The
   finding is never the number; it is what the number reveals: the function
   does three things, the error case is tangled with the happy one, a
   condition repeats.
6. **Buried happy path.** The whole body inside an opening `if`. Fix: invert
   the condition, return early, un-nest the rest (guard clause).
7. **Hidden bottleneck.** Query inside a loop (N+1), sequential `await` that
   could be `Promise.all`, synchronous I/O on the request path, unbounded
   fetch. **Where it runs changes the cost**: N+1 in a nightly job is often
   fine; the same N+1 in a controller is latency on every request.
8. **Tangle.** Circular imports (the hardest spaghetti signal) and inverted
   layer imports: a model importing a controller, a controller importing a
   sibling controller.
9. **Duplicated body.** Same code, different name (`clone`). Not "similar":
   identical. Fix: extract one function and parametrise the difference; if
   there is no difference, delete the copies.
10. **Swallowed error.** A `catch` that discards the exception, or only
    prints it. The failure vanishes and becomes a bug with no trace.
11. **Interpolated SQL.** A variable built into the query instead of a
    binding.
12. **Leftovers.** Dead imports, unreachable branches, flags nobody reads, and
    `orphan` / `test-only` from `--orphans`. **Careful**: calls built at
    runtime are invisible to that scan. Confirm before deleting.

### 3. What not to judge

- **A number alone is not a finding.** "CC=24" says nothing about what to do;
  "24 paths because it validates, converts and saves in one function: split
  the three" does. The number goes in as **evidence next to the reason**,
  never instead of it.
- **Length is not a defect.** A long linear function reads fine; a short
  tangled one does not. Counting lines measures what is easy to measure.
- **No style a linter settles.** Quotes, trailing commas, import order.
- **No preference without consequence.** "I would use map here" is not a
  finding.

A finding must answer **what breaks, or what gets expensive later**. If
neither, it is not a finding.

### 4. Report

Ordered by real cost, with `file:line`. For each: what it is, the evidence
(the other side of the duplication, the neighbour that diverges, the query in
the loop) and the concrete fix. A review with no findings is reported in one
line: that is a result, not a failure.

If something in the review deserves to become a repo rule, hand it to the
`conventions` skill: propose it, and wait for the user to accept.

## Maintenance

The symbol index rebuilds itself incrementally by mtime:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/csearch.py --rebuild
python3 ${CLAUDE_PLUGIN_ROOT}/csearch.py --stats
```

It is **always derived from disk**. If it disagrees with the code, it is
stale: rebuild it, do not correct it by hand.

The thresholds in `smell.py` (`CC_WARN`, `NEST_WARN`, `GUARD_MIN_LINES`) mark
where a human should look, not law. If one is noisy in a repo, adjust it in
the file rather than inventing exceptions in the report.
