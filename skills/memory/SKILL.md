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

## Proposals

At the end of each session the harness proposes changes to itself from
evidence: a recall page nobody read makes its matching word a **stopword**
candidate; a correction the model admitted becomes a draft **convention** for
the repo. They go to the proposals page with checkboxes.

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

Reads the transcripts and reports, per hook, how often the model acted on what
was injected. Measure before adding features: a hook that fails silently looks
exactly like one that works.
