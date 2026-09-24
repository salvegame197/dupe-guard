# qualidade-guard

A Claude Code hook that stops duplicated code **before it is written**, not after.

It runs on `PreToolUse` for `Write`, `Edit` and `MultiEdit`. Before the model
commits code to disk, the hook indexes the repository and asks two questions:

1. **Does a symbol with this name already exist?** (the easy half)
2. **Does this exact body already exist under a different name?** (the half a
   name search can never find)

If the answer is yes, it injects the locations into the model's context and
lets the model decide. It never blocks.

```
## Reuso: corpo duplicado

**`handleDebitError`** — este corpo (78 tokens) ja existe identico, com outro nome:
  - `src/pagamentos.js:2` → `handleCreditError`
```

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

`qualidade-guard` fingerprints function bodies: it tokenizes, drops identifiers
and literals, keeps structure (control flow, operators, punctuation), and
hashes the result. Bodies under 60 structural tokens are skipped — shallow
getters and CRUD wrappers collide by coincidence, and a false positive costs
more trust than a missed clone.

## Languages

TypeScript / JavaScript (`.ts .tsx .js .jsx .mjs .cjs .vue .svelte`), PHP,
Python, Rust, Go.

## Install

Requires Python 3.9+ and git. No dependencies outside the standard library.

```bash
git clone https://github.com/<you>/qualidade-guard
cd qualidade-guard
./instalar.sh
```

The installer registers the hook in `~/.claude/settings.json`, backing the file
up first. It refuses to run if a `qualidade-guard` entry is already registered.

To uninstall: `./desinstalar.sh`

## Where it writes

Everything goes to `~/.qualidade-guard/` (`cache/`, `estado/`, `convencoes/`).
Override with `QUALIDADE_GUARD_HOME`. It writes nothing into your repositories.

## Per-repo conventions

If `~/.qualidade-guard/convencoes/<repo-name>.md` exists, its contents are
injected once per session on the first code write. Use it for the rules a
linter cannot express. See `convencoes/_modelo.md`.

## Try it

```bash
QUALIDADE_GUARD_HOME=/tmp/qg-demo python3 guard.py < exemplos/payload.json
```

`exemplos/demo/` is a repository with a duplicate planted in it.

## Licence

MIT.
