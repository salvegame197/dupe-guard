# dupe-guard

A Claude Code hook that stops duplicated code **before it is written**, not after.

It runs on `PreToolUse` for `Write`, `Edit` and `MultiEdit`, and on
`PostToolUse` for `Bash`. Before the model commits code to disk (or, for Bash,
right after the command changed the repo), the hook indexes the repository and
asks two questions:

1. **Does a symbol with this name already exist?** (the easy half)
2. **Does this exact body already exist under a different name?** (the half a
   name search can never find)

If the answer is yes, it injects the locations into the model's context and
lets the model decide. It never blocks.

```
## Reuse: duplicated body

**`handleDebitError`** — this exact body (78 tokens) already exists under another name:
  - `src/payments.js:2` → `handleCreditError`
```

## Why Bash too

Measured on one real setup over eleven days: 50 writes went through `Write` or
`Edit`, **1,283 went through Bash** (`cat > file <<EOF`, `sed -i`,
`python - <<PY`). A guard that only listens to `Write`/`Edit` was blind to 96%
of the code. The Bash path cannot see the code beforehand, so it looks at
`git status` right after the command and analyses only the added lines of code
files changed in the last 90 seconds. Untracked files count whole.

## Why before and not after

The usual place to catch duplication is code review. By then the duplicated
function is already written, and the cost of removing it is social rather than
technical: somebody has to admit they rewrote something that existed.

Before the write, that cost is zero. It is simply not writing it.

## Design rules

Silence is the default. A hook that talks too much gets turned off, and a hook
that is turned off catches nothing — so every rule here exists to protect the
right to speak:

- **Speaks only above a threshold.** A weak suggestion spends context and
  derails attention.
- **Never repeats a warning** within a session.
- **Never blocks.** It injects context; the model decides.
- **Never stalls.** 2.5s budget. Over budget, it exits silently.
- **Fails silently, always.** A broken hook must never break an edit.
- **Ignores symbols the target file already declares.** Without this filter,
  editing a function warns you that the function exists — pointing at the line
  you are editing. That kind of noise is what gets hooks disabled.

## Clone detection

Name matching is the easy half. `handleCreditError` and `handleDebitError` are
the same body, and no name query relates them.

`dupe-guard` fingerprints function bodies: it tokenizes, drops identifiers
and literals, keeps structure (control flow, operators, punctuation), and
hashes the result. Bodies under 60 structural tokens are skipped — shallow
getters and CRUD wrappers collide by coincidence, and a false positive costs
more trust than a missed clone.

## Languages

TypeScript / JavaScript (`.ts .tsx .js .jsx .mjs .cjs .vue .svelte`), PHP,
Python, Rust, Go.

## Install

Requires Python 3.9+ and git. No dependencies outside the standard library.
Tested on 3.9, 3.11 and 3.12, macOS and Linux. The installer is a bash script.

```bash
git clone https://github.com/salvegame197/dupe-guard
cd dupe-guard
./install.sh
```

The installer registers the hook in `~/.claude/settings.json`, backing the file
up first. It records the absolute path of the Python that ran it, because Claude
Code may launch hooks with a reduced `PATH` where a bare `python3` resolves to a
system stub. It refuses to run if a guard is already registered.

To uninstall: `./uninstall.sh`

## Where it writes

Everything goes to `~/.dupe-guard/` (`cache/`, `state/`, `conventions/`).
Override with `DUPE_GUARD_HOME`. It writes nothing into your repositories.
Session state older than 7 days is pruned automatically.

The guard only acts inside git repositories. Outside one, the natural fallback
would be to index the file's parent folder, and for `~/Documents/x.py` that is
all of `~/Documents`.

## Per-repo conventions

If `~/.dupe-guard/conventions/<repo-name>.md` exists, its contents are
injected once per session on the first code write. Use it for the rules a
linter cannot express. See `conventions/_template.md`.

## Try it

```bash
DUPE_GUARD_HOME=/tmp/qg-demo python3 guard.py < examples/payload.json
```

`examples/demo/` is a repository with a duplicate planted in it.

## Tests

```bash
python3 -m unittest discover -s test -v
```

Twelve end-to-end cases run `guard.py` the way Claude Code does, JSON on stdin,
against a throwaway git repo: clone detected, silence on unrelated code, silence
on garbage input, other tools ignored, no repeated warning in a session, silence
outside git, old state pruned; and for the Bash path: new file caught after the
fact, clone appended to a tracked file caught, nothing changed is silent, `cd`
into a repo from elsewhere, stale changes ignored.

## Licence

MIT.
