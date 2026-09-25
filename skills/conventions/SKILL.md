---
name: conventions
description: Propose and maintain a repository's coding conventions (error handling, API responses, validation, dates, HTTP client, logging) from what the code actually does, with counts as proof. The rules are injected by the dupe-guard hook on the first code write of each session. Use when the user asks what the conventions of a repo are, wants to set or update project rules, asks "how is X done here", or after a review surfaces something that should become a rule.
---

# conventions — the rules a linter cannot express

`~/.dupe-guard/conventions/<repo-name>.md` holds what has been **confirmed**
about a repository. The dupe-guard hook injects that file on the first code
write of each session, so what goes in there is known from then on without
anyone repeating it. (`DUPE_GUARD_HOME` moves the whole directory.)

It is the only hand-written part of the plugin, and therefore the only part
that can lie. Three rules keep it honest:

- **Only what the user confirmed**, never what the model inferred. A rule the
  model invents becomes permanent law without ever having been decided.
- **Only what is verifiable in the code**, with a file path. No path, no rule.
- **Few.** The whole file goes into context: five firm rules are worth more
  than twenty lukewarm ones.

## Starting a repo from zero

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/propose.py            # current repo
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/propose.py --repo <path>
```

It reads the code and prints candidates with the counts as proof. It **writes
nothing**. There are two ways to read what it returns:

- **one style dominates** (70%+) → rule candidate ("here it is always done
  this way");
- **several are tied** → the divergence **is** the finding. There is no rule
  to write, there is a decision to make: two styles living together cost more
  than either one.

It also flags **rival libraries** installed side by side (two date libraries,
two HTTP clients): the project pays for both and needs one.

## Writing a rule

1. Show the user the candidate with its evidence (count and a file path).
2. **Wait for acceptance.** Do not write the file on your own.
3. Write it in the repo's file, following the template at
   `${CLAUDE_PLUGIN_ROOT}/conventions/_template.md`. Create the file from the
   template if it does not exist; the file name is the repository folder name.

## When the code contradicts a rule

The code wins, always. Correct the rule right away, marking what changed
instead of deleting it:

```markdown
> ⚠️ **Superseded on YYYY-MM-DD**: <what changed>
```

If you got it wrong, the damage was a wrong label. If you delete it, the
information is gone.
