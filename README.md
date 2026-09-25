# dupe-guard

A Claude Code plugin with two jobs:

- **Stop duplicated code before it is written**, not after (the guard, below).
- **Give the model a second brain**: sessions recorded to a markdown vault,
  recalled when a prompt touches a known subject, distilled into project notes,
  and a harness that measures itself and proposes its own corrections
  ([Second brain](#second-brain)). Off until you enable it.

## The guard

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

## What's in the plugin

| Part | What it does |
|---|---|
| **Hook** | Warns before `Write`/`Edit`, and after any `Bash` command that changed code, when the new code already exists: same name, or identical body under another name. Never blocks. |
| **`/dupe-guard:review`** | A code review skill: duplication, complexity, missing guard clauses, N+1 queries, sequential `await`, swallowed errors, interpolated SQL, circular imports, layer violations, dead code. The scripts point at addresses; the skill tells the model how to judge them. |
| **`/dupe-guard:conventions`** | Reads the repo and proposes its conventions (error handling, API responses, validation, dates, HTTP client, logging) with counts as proof. Writes nothing without your approval. Accepted rules are injected by the hook on the first code write of each session. |
| **Second brain hooks** | Session index, recall, reindex, session log, proposals. Off until enabled. |
| **`/dupe-guard:memory`** | Sets the second brain up, and tells the model how to use the vault, distill sessions and handle proposals. |

## Install, update, uninstall

Requires Python 3.9+ and git. No dependencies outside the standard library.
Tested on 3.9, 3.11 and 3.12, macOS and Linux. Either path installs the same
thing, the guard and the second brain (which stays off until you enable it).
**Pick one**: with both, every hook runs twice.

### As a Claude Code plugin

```
/plugin marketplace add salvegame197/dupe-guard
/plugin install dupe-guard@dupe-guard
```

Update, then restart Claude Code:

```
/plugin marketplace update dupe-guard
/plugin update dupe-guard@dupe-guard
```

Uninstall: `/plugin uninstall dupe-guard`.

The hooks call `python3` from `PATH`. Everything is standard library, so any
Python 3.9+ works, including the one macOS ships with the developer tools.

### Without the plugin system

```bash
git clone https://github.com/salvegame197/dupe-guard
cd dupe-guard
./install.sh
```

`install.sh` registers every hook `hooks/hooks.json` declares in
`~/.claude/settings.json`, pointing at this folder and at the absolute path of
the Python that ran it. It backs the file up first and touches no other entry.

It is a sync, not a one-shot: to update, `git pull && ./install.sh`. Hooks
that were added, changed or removed upstream are applied; nothing duplicates.
It refuses to run while another guard or the dupe-guard plugin is active.

`./uninstall.sh` removes every entry pointing at this folder and nothing else.

### What uninstalling leaves behind

Neither path deletes data. What remains, and whether it is safe to remove:

| Where | What | Delete? |
|---|---|---|
| `~/.dupe-guard/` | config, repo conventions, search index, state | safe; you lose the config and conventions |
| your vault (`~/second-brain` by default) | your notes | keep them |

The vault is deliberately **outside** `~/.dupe-guard`, so cleaning the data
folder can never take the notes with it.

### Releasing (maintainers)

`/plugin update` only moves users forward when the version changes; code
pushed without a bump never reaches anyone, and the update just says "already
at the latest version". So every release goes through:

```bash
python3 scripts/release.py 0.4.0 --push
```

It bumps both manifests, runs the tests (and leaves the version alone if they
fail), commits, and tags with `claude plugin tag`, which checks that the two
manifests agree.

## Second brain

A model's memory ends with the session, and local transcripts are deleted after
a few weeks. What was decided, what broke and why, which alternative was
rejected: gone, unless someone remembers to write it down. Nobody does.

The second brain writes it down for you, into a folder of markdown notes (the
**vault**, which Obsidian opens as is, though nothing requires Obsidian):

```
session starts   vault_context       compact index of the vault, never the whole vault
                 proposals_context   the harness's own proposals, waiting for you
every prompt     recall              relevant notes injected, only above a threshold
end of a turn    reindex             search index refreshed, at most every 15 minutes
session ends     session_log         the verifiable trace: requests, files, commits, tokens
on demand        distill             a model turns the trace into decisions and pitfalls
                                     inside the project's note
```

Two levels, deliberately separate. `session_log` is cheap and deterministic
(no model, ~100 ms, always runs) and records only what is verifiable. `distill`
needs a model, so it only runs when asked; it calls `claude -p`, your own
Claude Code, with no API key to configure. It shows a dry-run diff first,
stops after the first note it writes so you can review it, and after each
insertion asks several samples whether the new block made an older section
false, marking that section as superseded (never deleting it) when a majority
agrees.

### A harness that corrects itself

Hooks that fail do so silently, and a silent hook looks exactly like a working
one. Two tools deal with that:

- **`hit_rate.py`** reads the transcripts and reports, per hook, how often the
  model acted on what was injected. On the setup this was built on, it found
  the guard had been blind to 96% of writes and that recall pages were almost
  never opened.
- **Proposals.** At the end of each session the harness proposes changes to
  itself from evidence: a recalled page nobody read makes the word that matched
  it a stopword candidate; a correction the model admitted becomes a draft
  convention for the repo. They land on a page in the vault with checkboxes.
  **Nothing applies itself**: you decide (in chat is fine), then
  `proposals.py --apply`.

### Enable it

Ask Claude to set it up (`/dupe-guard:memory`), or write
`~/.dupe-guard/config.json` yourself:

```json
{
  "brain": {
    "enabled": true,
    "vault": "~/second-brain",
    "languages": ["en"],
    "note_language": "English"
  }
}
```

then `python3 <plugin>/brain/ksearch.py --reindex` and start a new session.
English and Portuguese are supported: the language tunes recall's stopword
lists and the correction phrases proposals look for. See the `memory` skill
for every option.

### Privacy

Everything stays on your machine: the vault, the index, the queue. The only
thing that leaves it is what `distill` sends to Claude when you run it (the
project note and that session's conversation), through your own Claude Code.

## Where it writes

Everything goes to `~/.dupe-guard/` (`cache/`, `state/`, `conventions/`, and
`brain/` for the second brain's index and queue), except the vault itself,
which defaults to `~/second-brain`.
Override with `DUPE_GUARD_HOME`. It writes nothing into your repositories.
Session state older than 7 days is pruned automatically.

The guard only acts inside git repositories. Outside one, the natural fallback
would be to index the file's parent folder, and for `~/Documents/x.py` that is
all of `~/Documents`.

## Per-repo conventions

If `~/.dupe-guard/conventions/<repo-name>.md` exists, its contents are
injected once per session on the first code write. Use it for the rules a
linter cannot express. Start one with `/dupe-guard:conventions`, or by hand
from `conventions/_template.md`.

## Try it

```bash
DUPE_GUARD_HOME=/tmp/qg-demo python3 guard.py < examples/payload.json
```

`examples/demo/` is a repository with a duplicate planted in it.

## Tests

```bash
python3 -m unittest discover -s test -v
```

The hook has thirteen end-to-end cases run `guard.py` the way Claude Code does, JSON on stdin,
against a throwaway git repo: clone detected, silence on unrelated code, silence
on garbage input, other tools ignored, no repeated warning in a session, silence
outside git, old state pruned, test files skipped; and for the Bash path: new
file caught after the fact, clone appended to a tracked file caught, nothing
changed is silent, `cd` into a repo from elsewhere, stale changes ignored.

The second brain has seventeen: off until enabled, the session log (recorded,
queued, idempotent, silent without substance), the vault index, search in a
hidden-folder vault, recall speaking and staying quiet, proposals from an unread
page and from an admitted correction, and `distill` against a fake `claude`
(dry run, insertion before the closing section with the session's date, one
retry on bad format, giving up after two, EMPTY, staleness needing a majority,
commits without trailers).

The install path has seven: everything `hooks.json` declares gets registered,
running it again syncs without duplicating, an older install picks up new
hooks, it refuses with another guard or with the plugin enabled, and uninstall
removes only its own entries and deletes no data.

The skill scripts and manifests have nine more: `difflook` on the working diff
and on a branch, `smell` finding an N+1, `propose` drafting without writing, and
the manifests, hook paths and skill references all resolving to real files.

## Licence

MIT.
