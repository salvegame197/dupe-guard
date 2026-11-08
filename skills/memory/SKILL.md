---
name: memory
description: The second brain — Claude Code sessions recorded to a markdown vault (Obsidian-compatible), recalled when a prompt touches a known subject, and distilled into project notes. Use when the user wants to set up or configure memory between sessions, asks "do you remember", "did we do this before", "how was that", wants to distill or review recorded sessions, decide on harness proposals, or measure whether the hooks help.
---

# memory — what was learned survives the session

Local transcripts are deleted after a few weeks, and a model's memory ends with
the session. The vault is the only durable store: plain markdown with
`[[wikilinks]]` and YAML frontmatter, readable in Obsidian or any editor.

| When | Hook | What happens |
|---|---|---|
| session starts | `vault_context` | a compact index of the vault (never the whole vault) |
| session starts | `proposals_context` | harness proposals waiting for a decision |
| every prompt | `recall` | relevant pages injected, only above a threshold, never twice |
| end of a turn | `reindex` | search index refreshed, at most every 15 minutes |
| session ends | `session_log` | the verifiable trace written to `Sessions/Raw/<day>.md`, queued for distilling |

Nothing runs until it is enabled.

## Setup

Ask the user two things, then write the config:

1. **Where is the vault?** An existing Obsidian vault, or a new folder. The
   default is `~/second-brain`.
2. **Which language do they work in?** `en` and `pt` are supported. The first
   one sets the labels written to the vault; all of them tune recall.

Write `~/.dupe-guard/config.json` (merge if it exists; `DUPE_GUARD_HOME` moves
it). Never put the vault inside `~/.dupe-guard`: that folder holds only index,
cache and state, and is safe to delete.

```json
{
  "brain": {
    "enabled": true,
    "vault": "~/path/to/vault",
    "languages": ["en"],
    "note_language": "English"
  }
}
```

Optional keys: `project_aliases` (repo folder name → note name, when they
differ), `no_project_dirs` (folders that mean "no project"), `raw_dir`
(default `Sessions/Raw`), `proposals_page` (default `Harness Proposals.md`),
`context_sections` (which folders the session index lists; by default every
top-level folder), `distill_model` (default `sonnet`).

Then build the search index and tell the user to start a new session:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/brain/ksearch.py --reindex
```

A project gets distilled notes once the vault has a page named after the repo
folder (or an alias). Until then its sessions stay in the raw log only.

## Using the vault

- **Before investigating a project from scratch, search the vault.** The index
  at session start says what exists; grep or `ksearch` finds it:
  `python3 ${CLAUDE_PLUGIN_ROOT}/brain/ksearch.py <terms>` searches the vault
  and past transcripts, ranked.
- **A note is a hypothesis, the code is the truth.** When a note contradicts
  the repository, the code wins. Correct the note right away, marking instead
  of deleting: `> ⚠️ **Superseded on YYYY-MM-DD**: <what changed>`.
- Recall output is a reminder that may be out of date. Confirm before acting.

## Distilling

The raw log records what is verifiable. Decisions, root causes and pitfalls
need a model: `distill.py` reads the queue and asks `claude -p` for a block per
session, which counts against the user's plan like any session.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/brain/distill.py                  # dry run: shows diffs
python3 ${CLAUDE_PLUGIN_ROOT}/brain/distill.py --apply --commit # writes, one commit per note
```

It stops after the first note written (the gate) so the user can review it;
`--no-gate` continues. After inserting, it asks several samples whether the new
block made any older section false, and marks those sections as superseded
when a majority agrees. `--stale <note>` runs only that check.

Run it when the user asks. Show the dry run first.

### Unattended

With `"distill_auto": true` in the config, the Stop hook starts a detached
worker once `distill_min_pending` sessions are waiting (or the oldest waited
`distill_max_age_hours`). It distils up to `distill_batch` sessions, commits
one note at a time when the vault is a git repository, and the next session
start shows what was written, with the command to revert each note. Lint
problems and errors show there too. Suggest turning it on only after the
user has seen a few manual dry runs and liked them; it spends their plan.

## Freshness

Notes state facts the disk can check. `freshness.py` takes every
`backtick/path.ext` in a project note and looks for it in every known
repository (working tree and every local branch, root or under `src/`,
`app/`, `lib/`, `resources/`). A path no known repo has becomes a `stale`
proposal; applying it marks that section as superseded, never deletes.

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/brain/freshness.py            # report
python3 ${CLAUDE_PLUGIN_ROOT}/brain/freshness.py --record   # write the proposals
```

The repository of a note is the last session folder that mapped to it, or
`note_repos` in the config. A line that says the file was removed or renamed
is not a stale fact and is skipped.

## Meaning, not words (optional)

Keyword search cannot reach paraphrase: "watch" for a page that only says
"relogio". Local embeddings can. They need an [Ollama](https://ollama.com)
server on the machine with a multilingual model:

```bash
brew install ollama && brew services start ollama    # or the Linux installer
ollama pull bge-m3
python3 ${CLAUDE_PLUGIN_ROOT}/brain/embed.py --build  # encodes the index; incremental after
```

then `"embeddings": {"enabled": true}` in the config. The reindex hook keeps
the vectors current. Nothing leaves the machine, no key. When the server is
down, search falls back to keywords on its own. Measure with `truth.py`
before and after turning it on; that is what decides whether it earns its
place on a given vault.

## Known truths

`truth.py` scores recall against questions with known answers, kept on a
page in the vault (`truth_page`, a markdown table with `question` and
`expected page` columns; several pages separated by ` / `). Run it before
and after any change to recall: `--save` keeps the run, `--diff` compares
the last two. hit, miss and silent are counted apart on purpose.

## Learned from corrections

At the end of each session `learned.py` looks for the triad "the user
corrected or stated a rule, the model admitted or accepted it, the model
edited something in that turn" and writes ONE draft line per triad (at most
three per session, the user's own words, dated, with the session id):

- in the repo's `CLAUDE.md`, section `## Aprendido (revisar)` (`## Learned
  (review)` in English), newest first, when the repo is the user's own (no
  remote, or a remote under `learned.own_remotes`, e.g.
  `["github.com/me/"]`);
- in `~/.claude/projects/<folder>/memory/aprendido-<repo>.md` (type
  `feedback`, pointer in `MEMORY.md`) when the repo belongs to someone else
  or there is no repo.

Approving is the commit the user makes anyway; rejecting is deleting the
line. A `CLAUDE.md` with someone's uncommitted change is never touched: the
line waits in `brain/aprendido-pendente.jsonl` and is retried at the next
session end. Everything is logged in `brain/aprendido-log.jsonl`;
`hit_rate.py` reports what survived. `learned.py --dry-run` shows what the
transcripts on disk would produce, writing nothing. Config: `learned:
{"enabled": true, "own_remotes": [], "max_per_session": 3}`.

## Rules in force

The distiller returns, after the dated block, up to five one-line rules
(a decision, a pitfall, a "do not"); they go to a fixed `## Regras em vigor`
(`## Rules in force`) section right after the note's title and summary,
newest first, each with the date and a `[[note#section]]` link to the block
it came from. Cap of 25; a rule superseded by a newer one, or whose section
was marked superseded, moves to `## Regras superadas` at the end of the
note, dated, never deleted. `vault_context` injects those lines at session
start for the repo's note, and `recall` takes its snippet from them first.

## Proposals

The proposals page now only receives `stale` items (from `freshness.py`).
`convention` lines are no longer generated (see "Learned from
corrections"); `stopword` neither. Lines already on the page, of any kind,
are still applied by `--apply`.

**Never apply a proposal on your own.** Show them, give an opinion per item
("not reading a page is not the same as it being irrelevant"), and let the user
decide in chat. Then mark `[x]` / `[-]` on the page and run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/brain/proposals.py --apply
```

## Measuring

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/brain/hit_rate.py
```

Reads the transcripts and reports, per hook, what happened after each
injection: a lead (`path` or `symbol`) from the snippet used by a tool in the
same turn, the guard's candidate edited, a correction in the next human
prompt next to the repo's base rate, a manual `ksearch`, a rule written to
memory or CLAUDE.md; per page and per repo. `--strength` rewrites the table
recall uses to raise or lower pages from those outcomes. Measure before adding
features: a hook that fails silently looks exactly like one that works.
