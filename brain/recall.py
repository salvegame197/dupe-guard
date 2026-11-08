#!/usr/bin/env python3
"""
Hook UserPromptSubmit: automatic recall.

When a prompt is about something there is already accumulated knowledge on, it
injects the best findings into context, without waiting for the model to think
of searching.

Design rules (silence is the default):
  - Only answers a PERSON. Task notifications, system messages, pasted skill
    text and context-continuation summaries go through this hook too (907 of
    2,673 prompts in 28 days, measured); they are skipped before any search.
  - Only fires on prompts with substance. "ok", "go ahead" trigger nothing.
  - In a code session (cwd inside a git repo, or the guard saw an edit in a
    repo this session) the chat exports in `Claude Web/` are muted: 59 % of
    what was injected came from there and none of it was about the code.
  - The native memory of the session's own project folder is never injected:
    Claude Code already loads it. Memory from OTHER folders is, labelled with
    where it came from ("memory of gate-control").
  - When the note of the session's repo has a "Rules in force" section (the
    distiller keeps it at the top, brain/rules.py), the snippet comes from
    there; for any other note, from there only when a term matches in it.
  - Only injects above a score threshold. A weak result is worse than none: it
    spends context and pulls attention away.
  - Never repeats what it already injected in the same session.
  - Always fails silently: a noisy hook is worse than a missing one.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import (DATA, DEFAULTS, HOME_DIR, fold, hook_output, labels,     # noqa: E402
                        load_config, project_folder_name, redact)

STATE_DIR = DATA / "recall-state"
GUARD_STATE_DIR = HOME_DIR / "state"          # written by guard.py, read here
MANUAL_STOPWORDS = DATA / "recall-stopwords.json"
STRENGTH = DATA / "recall-strength.json"      # written by hit_rate.py --strength
LOG = DATA / "recall-log.jsonl"
MAX_SNIPPETS = 1           # one snippet per page keeps an injection near 700 chars
MAX_LOG_LINES = 5000

MIN_PROMPT_CHARS = 25      # below this it is an acknowledgement, not a question
MIN_PROMPT_WORDS = 3       # useful words (3+ letters, outside code and URLs)

# What arrives in `prompt` without a person having typed it. Each is the
# opening of the text as Claude Code writes it: background task results,
# system and local-command envelopes, images, the summary that opens a
# resumed session, a slash command, a skill's SKILL.md pasted whole (a
# <command-message> envelope or its YAML frontmatter), cross-session context.
NON_HUMAN_PREFIXES = (
    ("task-notification", "<task-notification"),
    ("system", "<system"),
    ("local-command", "<local-command"),
    ("image", "[Image"),
    ("continuation", "This session is being continued"),
    ("command", "<command-name>"),
    ("skill", "<command-message>"),
    ("skill", "---\nname:"),
    ("cross-session", "<cross-session"),
)
MIN_SCORE_TITLE = 7.0      # with the TITLE required to match (see pick), a
                           # modest score is enough; the score only ranks.
MIN_SOLO_TERM = 6          # a single term only counts if it is specific
MAX_RESULTS = 3
MAX_TERMS = 6
MIN_TITLE_TERM = 5         # a short term matches half the corpus; proves nothing

# Words that appear everywhere and discriminate nothing, per language. Without
# them "how do I do the payment" would also search for "how" and "do".
STOP = {
    "en": {
        "the", "and", "for", "with", "without", "from", "into", "onto", "about",
        "this", "that", "these", "those", "there", "here", "then", "than", "them",
        "they", "their", "what", "when", "where", "which", "while", "who", "whom",
        "why", "how", "does", "doing", "done", "have", "has", "had", "having",
        "been", "being", "were", "was", "are", "will", "would", "could", "should",
        "might", "must", "can", "cannot", "just", "also", "still", "already",
        "again", "some", "any", "all", "each", "every", "more", "less", "most",
        "much", "many", "very", "really", "thing", "things", "something",
        "anything", "stuff", "kind", "sort", "like", "want", "need", "please",
        "okay", "yes", "maybe", "think", "look", "see", "make", "made", "take",
        "get", "got", "put", "use", "using", "used", "file", "files", "code",
        "problem", "issue", "error", "part", "way", "time", "before", "after",
        "inside", "better", "worse", "new", "old", "other", "same", "right",
        "wrong", "change", "changes", "fix", "work", "works", "working",
    },
    "pt": {
        "a", "o", "as", "os", "um", "uma", "de", "da", "do", "das", "dos", "e", "ou",
        "que", "em", "no", "na", "nos", "nas", "por", "para", "pra", "com", "sem",
        "se", "ao", "aos", "the", "eu", "voce", "vc", "tu", "me", "te", "meu",
        "minha", "seu", "sua", "isso", "isto", "esse", "essa", "este", "esta",
        "aquilo", "ali", "la", "aqui", "tem", "ter", "tinha", "ser", "estar", "sao",
        "estao", "foi", "era", "vai", "vou", "quer", "quero", "pode", "podemos",
        "fazer", "faz", "feito", "como", "onde", "quando", "porque", "qual",
        "quais", "mais", "menos", "muito", "pouco", "bem", "mal", "so", "tambem",
        "ja", "ainda", "agora", "entao", "mas", "porem", "sabe", "tipo", "coisa",
        "algo", "alguma", "algum", "certo", "errado", "errada", "mexer",
        "mexendo", "ajustar", "ajuste", "problema", "erro", "parte", "forma",
        "jeito", "maneira", "hora", "vezes", "antes", "depois", "dentro", "sobre",
        "assim", "melhor", "pior", "novo", "nova", "velho", "outro", "outra",
        "mesmo", "ok", "sim", "nao", "talvez", "acho", "olha", "veja", "ver",
        "file", "arquivo", "code", "codigo", "gente", "cara", "preciso",
        "precisa", "gostaria", "favor",
    },
}

# Meta-conversation verbs: they talk about the tool or the dialogue, never
# about a subject. "do you load something when a chat starts" has no subject
# to recall; without this, 'load' and 'starts' become search terms.
META = {
    "en": {
        "load", "loads", "loading", "start", "starts", "starting", "begin",
        "conversation", "conversations", "chat", "prompt", "message", "messages",
        "question", "ask", "answer", "reply", "explain", "show", "tell", "say",
        "write", "run", "open", "save", "remember", "understand", "knew", "check",
        "verify",
    },
    "pt": {
        "carrega", "carregar", "carregando", "inicia", "iniciar", "inicio",
        "comeca", "comecar", "conversa", "conversas", "conversar", "chat",
        "prompt", "mensagem", "mensagens", "pergunta", "perguntar", "pergunte",
        "responde", "responder", "resposta", "explica", "explicar", "explique",
        "mostra", "mostrar", "mostre", "fala", "falar", "diz", "dizer", "escreve",
        "escrever", "escreva", "roda", "rodar", "rode", "abre", "abrir", "salva",
        "salvar", "lembra", "lembrar", "entende", "entender", "sabia", "consegue",
        "conseguir", "checa", "checar", "confere", "conferir", "funciona",
        "funcionar", "serve", "servir", "usa", "usar", "use",
    },
}

# Conjugated verbs and adverbs are not subjects, and IDF does not catch them:
# a rare verb looks discriminating but names nothing. Only languages where the
# endings are unambiguous get this filter.
CONVERSATIONAL_ENDINGS = {
    "pt": ("ria", "riam", "rias", "ando", "endo", "indo", "aria", "eria", "asse",
           "esse", "isse", "aram", "eram", "iram", "amos", "emos", "mente"),
}


def load_set(path: Path) -> set:
    try:
        return set(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return set()


def non_human(prompt: str) -> str:
    """Why this prompt is not a person asking something, or "" when it is.
    Decided on the raw text, before any search: the terms a notification
    yields ('task-notification', 'tool-use-id', 'output-file') were the most
    frequent search terms in the log, and every one of them was a waste."""
    text = (prompt or "").lstrip()
    if not text:
        return "empty"
    for reason, prefix in NON_HUMAN_PREFIXES:
        if text.startswith(prefix):
            return reason
    if len(text) < MIN_PROMPT_CHARS:
        return "short"
    # Useful words: outside code, URLs and paths, three letters or more.
    body = re.sub(r"```.*?```", " ", text, flags=re.S)
    body = re.sub(r"`[^`]*`|https?://\S+|[/~][\w./\-]{6,}", " ", body)
    if len(re.findall(r"[^\W\d_]{3,}", body)) < MIN_PROMPT_WORDS:
        return "short"
    return ""


def in_git_repo(cwd: str) -> bool:
    """cwd inside a git worktree, found by walking up for `.git` (a folder,
    or the file a worktree leaves). No subprocess: this runs on every
    prompt. A `.git` in the home folder itself (dotfiles) does not count."""
    if not cwd:
        return False
    try:
        p = Path(cwd).resolve()
        home = Path.home().resolve()
    except (OSError, RuntimeError):
        return False
    for d in (p, *p.parents):
        if d == home:
            return False
        if (d / ".git").exists():
            return True
    return False


def guard_repos(session_id: str) -> list:
    """Repos the guard saw an edit in during this session (guard.py records
    them); the session may have started outside any repo."""
    if not session_id:
        return []
    try:
        st = json.loads((GUARD_STATE_DIR / f"{session_id[:64]}.json").read_text())
        return [r for r in st.get("repos") or [] if isinstance(r, str)]
    except (OSError, ValueError, TypeError, AttributeError):
        return []


def code_session(cwd: str, session_id: str) -> bool:
    """A session about code: the cwd is in a git repo, or something edited
    this session was. The recall mutes personal chat exports here."""
    return in_git_repo(cwd) or any(in_git_repo(r) for r in guard_repos(session_id))


def muted_prefixes(cfg: dict, code: bool) -> tuple:
    """Vault folders that get weight 0 in a code session."""
    return tuple(cfg.get("code_session_mute") or ()) if code else ()


def own_memory_prefix(cwd: str) -> str:
    """Index-id prefix of the native memory Claude Code loads for this cwd
    (`memory/<project folder>/`). The folder is keyed by the cwd as given,
    the way Claude Code keys it, so a session started in a subfolder has
    its own memory and the repo root's counts as another project's."""
    return f"memory/{project_folder_name(cwd)}/" if cwd else ""


def project_note(cwd: str, cfg) -> str:
    """The vault note the session's cwd maps to (alias, or a note with the
    folder's name), folded; "" when none."""
    if not cwd:
        return ""
    try:
        from brain.session_log import note_for
        name, orphan = note_for(cwd, cfg)
    except Exception:
        return ""
    return fold(name) if name and not orphan else ""


def rules_turn(doc: dict):
    """The index turn that is the note's "Rules in force" section, or None."""
    from brain.rules import IN_FORCE
    heads = tuple(f"## {h}" for h in IN_FORCE)
    return next((t for t in doc.get("turns") or () if t["t"].startswith(heads)), None)


def snippets_for(doc: dict, best, terms, is_project: bool) -> list:
    """The snippets to show for a page: the rules section first when this
    is the note of the session's repo, or when a term matches inside it;
    otherwise the best-matching turn as before. The rules text loses its
    heading and the `([[note#section]])` links to fit the snippet."""
    from brain import ksearch
    out = [ksearch.snippet(t["t"], terms) for t in best[:MAX_SNIPPETS]]
    turn = rules_turn(doc) if doc.get("src") == "vault" else None
    if turn:
        text = re.sub(r"^## [^-]*- ", "- ", turn["t"], count=1)
        text = re.sub(r" \(\[\[[^\]]+\]\]\)", "", text)      # the links name the note: not a match
        if is_project or any(x in fold(text) for x in terms):
            out.insert(0, ksearch.snippet(text, terms))
    return out[:MAX_SNIPPETS]


def terms_from(prompt: str, languages):
    """The terms with the most power to discriminate."""
    from brain.ksearch import STOPWORDS
    # Code and paths are implementation detail, not subject.
    prompt = re.sub(r"```.*?```", " ", prompt, flags=re.S)
    prompt = re.sub(r"`[^`]*`", " ", prompt)
    prompt = re.sub(r"https?://\S+", " ", prompt)
    prompt = re.sub(r"[/~][\w./\-]{6,}", " ", prompt)

    # The corpus stopwords are regenerated on every reindex, so words approved
    # through proposals live in a separate file that only `apply` changes.
    blocked = load_set(STOPWORDS) | load_set(MANUAL_STOPWORDS)
    endings = ()
    for lang in languages:
        blocked |= STOP.get(lang, set()) | META.get(lang, set())
        endings += CONVERSATIONAL_ENDINGS.get(lang, ())

    # An uppercase three-letter acronym is a subject (WAL, POS, API); a common
    # three-letter word is not. Decided BEFORE folding, which erases case.
    acronyms = {w.lower() for w in re.findall(r"\b[A-Z]{3}\b", prompt)}
    words = re.findall(r"[a-zA-ZÀ-ÿ][\w+#.-]{2,}", fold(prompt))
    seen, out = set(), []
    for w in words:
        # Trailing punctuation must not rescue a stopword ('here.' used to
        # slip past the filter for 'here' and push a useful term out). Only
        # EDGE punctuation is stripped, so 'v2.1' and 'index.ts' survive.
        w = w.strip(".,;:!?")
        if len(w) < 4 and w not in acronyms:
            continue
        if w in blocked or w in seen or (endings and w.endswith(endings)):
            continue
        seen.add(w)
        out.append(w)
    # The longest word tends to be the most specific.
    out.sort(key=len, reverse=True)
    return out[:MAX_TERMS]


def title_words(title: str) -> set:
    return set(re.findall(r"[a-z0-9]{3,}", fold(title)))


def title_hit(terms, title: str, aliases=None):
    """
    A term matching the title — by WORD, never by substring.

    'start' inside 'starter' is not a shared subject, it is a letter
    collision. The title is split into words and a term must match one whole,
    tolerating only a short plural inflection (up to 2 characters).

    Three cases the first version missed, each measured against questions
    with known answers:
      - a term with a hyphen or underscore ('shop-admin', 'gate_control')
        never matched, because the title splits into words and the term did
        not: every part of the term must now be a word of the title;
      - an acronym ('eua', 'wal', 'pos') was below the minimum length; an
        EXACT word match is now accepted for any term of 3+ letters, and only
        the inflection tolerance keeps the minimum;
      - a repo folder that is not the note's name ('gate_control' vs
        'Controle de Acesso') is bridged by the project aliases from config.
    """
    words = title_words(title)
    aliases = aliases or {}
    for t in terms:
        parts = [x for x in re.split(r"[-_]", t) if len(x) >= 3]
        if len(parts) > 1 and all(x in words for x in parts):
            return t
        alias_words = aliases.get(t)
        if alias_words and alias_words <= words:
            return t
        for w in words:
            if w == t:
                return t
            if len(t) < MIN_TITLE_TERM:
                continue
            short, long_ = (t, w) if len(t) <= len(w) else (w, t)
            if len(long_) - len(short) <= 2 and long_.startswith(short):
                return t
    return None


def alias_words(cfg) -> dict:
    """folded alias key -> words of the note it points at."""
    return {fold(k): title_words(v) for k, v in (cfg.get("project_aliases") or {}).items()}


def state_file(session_id: str) -> Path:
    return STATE_DIR / f"{re.sub(r'[^A-Za-z0-9_-]', '', session_id)}.json"


def already_seen(session_id: str) -> set:
    if not session_id:
        return set()
    try:
        return set(json.loads(state_file(session_id).read_text()))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def remember(session_id: str, ids: set) -> None:
    if not session_id or not ids:
        return
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        state_file(session_id).write_text(json.dumps(sorted(ids)))
    except OSError:
        pass


def pick(results, terms, seen, session_id, aliases=None, trusted=(), body_min=14.0, strong_cos=0.6):
    picked = []
    for r in results:
        if r["score"] < MIN_SCORE_TITLE:
            break             # ranked by score: the rest is below too
        if r["id"] in seen:
            continue
        # The CURRENT session is in the index after a recent reindex. Without
        # this the hook matches the prompt just written and returns itself.
        if session_id and r["id"] == session_id:
            continue
        # The TITLE must match a term. It is the deciding filter and the only
        # one that works: a noise word and a useful term can have the same
        # corpus frequency, so no IDF threshold separates them, while the title
        # is the subject already distilled.
        # A native memory note's one-line description is its subject as
        # much as the name is; both face the gate.
        subject = " ".join(x for x in (r.get("title"), r.get("desc")) if x)
        if not title_hit(terms, subject, aliases):
            # A curated note may still qualify on its body alone, with a
            # higher bar; a chat log or a session may not.
            curated = r["source"] == "vault" and any(r["id"].startswith(p) for p in trusted)
            # A strong vector match IS the title match that words could not
            # make: "relogio" next to "Apple Watch". That is the whole point.
            semantic = r.get("cos", 0.0) >= strong_cos or r.get("qhit", 0.0) >= 0.5
            if not ((curated and r["score"] >= body_min) or semantic):
                continue
        picked.append(r)
        if len(picked) >= MAX_RESULTS:
            break
    return picked


def recall(prompt: str, session_id: str, cfg: dict, cwd: str = ""):
    """(terms, matched, picked) for a prompt — the whole decision, with no
    side effects. The hook and the evaluation script both go through here, so
    what is measured is exactly what runs. `cwd` decides the code-session
    mode; the evaluation scripts pass none and get the plain behaviour."""
    from brain import ksearch
    why = non_human(prompt)
    if why:
        log_decision(session_id, prompt, [], [], [], why, decision="skip:non-human")
        return [], 0, []
    if not ksearch.INDEX.exists():
        return [], 0, []
    terms = terms_from(prompt, cfg["languages"])
    # One term alone is usually too generic, unless it is long and specific.
    if len(terms) < 2 and not (terms and len(terms[0]) >= MIN_SOLO_TERM):
        log_decision(session_id, prompt, terms, [], [], "too few terms")
        return terms, 0, []
    aliases = {fold(k): v for k, v in (cfg.get("project_aliases") or {}).items()}
    synonyms = {t: [aliases[t]] for t in terms if t in aliases}
    found, _ = ksearch.search(terms, limit=12, synonyms=synonyms)
    # Weight 0 in a code session: the chat exports are about the user's
    # life, and a prompt about code shares words with them by accident
    # ("lugar", "cliente", "api"). Dropped before the cut to twelve, so a
    # project note ranked below them still gets its turn, and again after
    # the vector side, so it cannot bring them back.
    code = code_session(cwd, session_id)
    muted = muted_prefixes(cfg, code)
    own = own_memory_prefix(cwd)

    def audible(src, doc_id):
        if src == "memory":
            return not (own and doc_id.startswith(own))   # Claude Code loads it already
        return not (muted and src == "vault" and doc_id.startswith(muted))

    found = [x for x in found if audible(x[1]["src"], x[1]["id"])]
    table = strength_table()
    mine = project_note(cwd, cfg)
    results = [{"score": round(weighted(s, d, cfg, terms) * strength_factor(d["id"], table), 1),
                "source": d["src"], "title": d["title"], "qhit": d.get("_qhit", 0.0),
                "desc": d.get("desc", ""), "kind": d.get("kind", ""),
                "project": d["project"], "date": d["date"], "id": d["id"],
                "snippets": snippets_for(d, b, terms, bool(mine) and d["src"] == "vault" and fold(d["title"]) == mine)}
               for s, d, b in found[:12]]
    results = [r for r in hybrid(results, prompt, terms, cfg, table) if audible(r["source"], r["id"])]
    results.sort(key=lambda x: -x["score"])
    picked = pick(results[:8], terms, already_seen(session_id), session_id, alias_words(cfg),
                  tuple(cfg.get("trusted_prefixes") or ()), cfg.get("body_match_min", 14.0),
                  (cfg.get("embeddings") or {}).get("strong_cos", 0.6))
    rejected = [x for x in results[:8] if x not in picked]
    log_decision(session_id, prompt, terms, picked, rejected,
                 "" if picked else ("no title match above threshold" if results else "nothing found"),
                 mode="code" if code else "chat", repo=Path(cwd).name if cwd else "")
    return terms, len(found), picked


def hybrid(results, prompt, terms, cfg, table):
    """Add the vector side when it is on: a bonus for documents keywords
    already found, and new entries for documents they missed. Everything
    downstream (title gate, trusted body match) applies to both alike."""
    e = cfg.get("embeddings") or {}
    if not e.get("enabled"):
        return results
    # Conservative, like the synonym expansion: when keywords already found a
    # strong title match there is nothing to rescue, and a bonus would only
    # add noise. Vectors are for what words missed.
    strong_kw = any(r["score"] >= 2 * MIN_SCORE_TITLE and title_hit(terms, r["title"]) for r in results[:3])
    if strong_kw:
        return results
    from brain import embed, ksearch
    near = embed.search(prompt, cfg)
    if not near:
        return results
    # bge-m3 cosines sit in a narrow band (right pages ~0.61, wrong ~0.56,
    # measured), so an absolute threshold separates nothing. What does is
    # STANDING OUT: the right page is first with a gap over the second.
    top_cos = near[0][1]
    second = near[1][1] if len(near) > 1 else 0.0
    stands_out = top_cos >= e.get("strong_cos", 0.6) and top_cos - second >= e.get("gap", 0.03)
    by_id = {r["id"]: r for r in results}
    docs = None
    for rank, (doc_id, cos) in enumerate(near):
        if cos < e.get("bonus_cos", 0.55):
            break
        bonus = round(e.get("weight", 30.0) * (cos - e.get("min_cos", 0.45)), 1)
        if doc_id in by_id:
            by_id[doc_id]["score"] = round(by_id[doc_id]["score"] + bonus, 1)
            by_id[doc_id]["cos"] = cos if (rank == 0 and stands_out) else 0.0
            continue
        if not (rank == 0 and stands_out):
            continue                      # rescue only the one that stands out
        if docs is None:
            docs = {d["id"]: d for d in ksearch.load_index()}
        d = docs.get(doc_id)
        if not d:
            continue
        best = max(d["turns"], key=lambda t: sum(t["t"].lower().count(w) for w in terms)) if d["turns"] else None
        results.append({"score": round(weighted(bonus, d, cfg, terms) * strength_factor(d["id"], table), 1),
                        "source": d["src"], "title": d["title"], "project": d["project"],
                        "date": d["date"], "id": d["id"], "cos": cos,
                        "snippets": [ksearch.snippet(best["t"], terms)] if best else []})
    return results


def strength_table() -> dict:
    try:
        return json.loads(STRENGTH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


STRENGTH_MIN_INJECTIONS = 5     # below this a correction rate is one event, not a rate
STRENGTH_MIN_CORRECTIONS = 2    # one correction after one injection proves nothing
CORRECTION_EXCESS_SOFT = 0.05   # corrected minus expected, per injection, that costs 0.8
CORRECTION_EXCESS_HARD = 0.15   # ... and 0.6


def strength_factor(doc_id: str, table: dict) -> float:
    """
    Memory strength fed by what happened AFTER the injection, not by whether
    the page was opened (it never is: 0 in 1,145, measured). hit_rate.py
    --strength derives the table from the transcripts; this only reads it.
    Per page the table holds `injected`, `touched` (a path or symbol from
    the injected snippet was used by a tool call in the same turn),
    `corrected` (the next human prompt was a correction) and `expected`
    (the sum, over the page's injections, of the correction base rate of
    the repo the session was in).

      factor = up * down
      up     = min(1 + 0.15 * touched, 1.6)             when touched > 0, else 1
      down   = 0.6 if excess >= 0.15, 0.8 if excess >= 0.05, else 1
               with excess = (corrected - expected) / injected,
               only when injected >= 5 and corrected >= 2 (one
               correction after one injection is an event, not a rate)
      no signal at all (never touched, corrections at the base rate) = 1.0

    A page that is injected many times and simply ignored stays neutral:
    "not opened" was the signal that made every page fade alike. Tables from
    before 2026-11-08 (`injected`, `opened` only) read as neutral.
    """
    s = table.get(doc_id)
    if not s:
        return 1.0
    inj = s.get("injected", 0) or 0
    touched = s.get("touched", 0) or 0
    up = min(1.0 + 0.15 * touched, 1.6) if touched else 1.0
    down = 1.0
    if inj >= STRENGTH_MIN_INJECTIONS and (s.get("corrected", 0) or 0) >= STRENGTH_MIN_CORRECTIONS:
        excess = ((s.get("corrected", 0) or 0) - (s.get("expected", 0.0) or 0.0)) / inj
        if excess >= CORRECTION_EXCESS_HARD:
            down = 0.6
        elif excess >= CORRECTION_EXCESS_SOFT:
            down = 0.8
    return round(up * down, 3)


def log_decision(session_id: str, prompt: str, terms, picked, rejected, reason: str,
                 decision: str = "", mode: str = "", repo: str = ""):
    """One line per decision, injected or not, with why. It is what lets a
    bad injection be explained a week later instead of guessed at. Decisions:
    inject, silent, skip:non-human (never searched). `mode` is code or chat;
    `repo` the cwd's basename; `picked` carries the index id too, so a later
    count by vault folder does not have to guess from a title."""
    if not session_id:
        return              # evaluation runs (truth.py) pass no session
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > MAX_LOG_LINES * 300:
            lines = LOG.read_text(encoding="utf-8").splitlines()[-MAX_LOG_LINES // 2:]
            LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        row = {"ts": int(time.time()), "session": session_id[:8], "prompt": redact(prompt)[:80],
               "terms": terms, "decision": decision or ("inject" if picked else "silent"),
               "reason": reason,
               "picked": [(p["title"][:60], p["score"]) for p in picked],
               "rejected": [(p["title"][:60], p["score"]) for p in rejected[:3]]}
        if mode:
            row["mode"] = mode
        if repo:
            row["repo"] = repo
        if picked:
            row["ids"] = [p["id"] for p in picked]
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError:
        pass


def weighted(score: float, doc: dict, cfg: dict, terms=()) -> float:
    """Relevance times importance times recency (the three of Generative
    Agents). Importance comes from config by origin or vault folder prefix,
    longest prefix wins; recency from the document's date."""
    imp = cfg.get("importance") or {}
    w = 1.0
    # Canonical page: a term that IS the note's name (or the alias that maps
    # to it). Asking about X when a page called X exists makes that page the
    # answer's home, and it must beat the many sessions that merely mention X.
    if doc["src"] == "vault" and terms:
        title = fold(doc["title"])
        names = set(terms) | {fold(v) for k, v in (cfg.get("project_aliases") or {}).items() if fold(k) in terms}
        if any(n == title or (len(n) >= 5 and title.startswith(n + "-")) for n in names):
            w *= 2.0
    if doc["src"] == "vault":
        # Longest matching folder prefix wins; "vault" is the fallback. This
        # multiplies into w: assigning here silently dropped the canonical x2.
        best, factor = "", imp.get("vault", 1.0)
        for prefix, weight in imp.items():
            if prefix not in ("vault", "memory", "memory_feedback") and doc["id"].startswith(prefix) \
                    and len(prefix) > len(best):
                best, factor = prefix, weight
        w *= factor
    elif doc["src"] == "memory":
        # A config that lists its own `importance` rarely names these keys;
        # the defaults apply so the native memory keeps outranking the vault.
        base = DEFAULTS["importance"]
        w *= imp.get("memory", base["memory"])
        if doc.get("kind") == "feedback":
            w *= imp.get("memory_feedback", base["memory_feedback"])
    else:
        w *= imp.get("transcript", 1.0)
    if cfg.get("recency_boost") and doc.get("date"):
        try:
            age = (time.time() - time.mktime(time.strptime(doc["date"], "%Y-%m-%d"))) / 86400
            w *= 1.15 if age <= 7 else 1.08 if age <= 30 else 1.0
        except (ValueError, OverflowError):
            pass
    return score * w


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    cfg = load_config()
    if not cfg["enabled"]:
        return 0
    prompt = payload.get("prompt") or ""
    session_id = payload.get("session_id") or ""
    terms, matched, picked = recall(prompt, session_id, cfg, payload.get("cwd") or os.getcwd())
    if not picked:
        return 0

    L = labels(cfg)
    lines = [f"## {L['recall_header']}", "",
             f"{L['recall_search']}: {', '.join(terms)} — {matched} {L['recall_found']}", ""]
    for r in picked:
        if r["source"] == "vault":
            origin = "vault"
        elif r["source"] == "memory":
            origin = L["recall_memory_of"].format(p=r.get("project") or "?")
        else:
            origin = r.get("project") or "?"
        when = f", {r['date']}" if r.get("date") else ""
        lines.append(f"**{r['title']}** ({origin}{when})")
        for s in r.get("snippets", [])[:MAX_SNIPPETS]:
            lines.append(f"  - {s}")
        lines.append("")
    lines.append(L["recall_footer"] + f" `python3 {Path(__file__).parent / 'ksearch.py'} <terms>`")

    remember(session_id, already_seen(session_id) | {r["id"] for r in picked})
    print(hook_output("UserPromptSubmit", "\n".join(lines)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
