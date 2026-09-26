#!/usr/bin/env python3
"""
Hook UserPromptSubmit: automatic recall.

When a prompt is about something there is already accumulated knowledge on, it
injects the best findings into context, without waiting for the model to think
of searching.

Design rules (silence is the default):
  - Only fires on prompts with substance. "ok", "go ahead" trigger nothing.
  - Only injects above a score threshold. A weak result is worse than none: it
    spends context and pulls attention away.
  - Never repeats what it already injected in the same session.
  - Always fails silently: a noisy hook is worse than a missing one.
"""

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, fold, hook_output, labels, load_config   # noqa: E402

STATE_DIR = DATA / "recall-state"
MANUAL_STOPWORDS = DATA / "recall-stopwords.json"
STRENGTH = DATA / "recall-strength.json"      # written by hit_rate.py --strength
LOG = DATA / "recall-log.jsonl"
MAX_SNIPPETS = 1           # one snippet per page keeps an injection near 700 chars
MAX_LOG_LINES = 5000

MIN_PROMPT_CHARS = 25      # below this it is an acknowledgement, not a question
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
        if not title_hit(terms, r["title"] or "", aliases):
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


def recall(prompt: str, session_id: str, cfg: dict):
    """(terms, matched, picked) for a prompt — the whole decision, with no
    side effects. The hook and the evaluation script both go through here, so
    what is measured is exactly what runs."""
    from brain import ksearch
    if len(prompt.strip()) < MIN_PROMPT_CHARS or not ksearch.INDEX.exists():
        return [], 0, []
    terms = terms_from(prompt, cfg["languages"])
    # One term alone is usually too generic, unless it is long and specific.
    if len(terms) < 2 and not (terms and len(terms[0]) >= MIN_SOLO_TERM):
        log_decision(session_id, prompt, terms, [], [], "too few terms")
        return terms, 0, []
    aliases = {fold(k): v for k, v in (cfg.get("project_aliases") or {}).items()}
    synonyms = {t: [aliases[t]] for t in terms if t in aliases}
    found, _ = ksearch.search(terms, limit=12, synonyms=synonyms)
    table = strength_table()
    results = [{"score": round(weighted(s, d, cfg, terms) * strength_factor(d["id"], table), 1),
                "source": d["src"], "title": d["title"], "qhit": d.get("_qhit", 0.0),
                "project": d["project"], "date": d["date"], "id": d["id"],
                "snippets": [ksearch.snippet(t["t"], terms) for t in b[:MAX_SNIPPETS]]}
               for s, d, b in found[:12]]
    results = hybrid(results, prompt, terms, cfg, table)
    results.sort(key=lambda x: -x["score"])
    picked = pick(results[:8], terms, already_seen(session_id), session_id, alias_words(cfg),
                  tuple(cfg.get("trusted_prefixes") or ()), cfg.get("body_match_min", 14.0),
                  (cfg.get("embeddings") or {}).get("strong_cos", 0.6))
    rejected = [x for x in results[:8] if x not in picked]
    log_decision(session_id, prompt, terms, picked, rejected,
                 "" if picked else ("no title match above threshold" if results else "nothing found"))
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


def strength_factor(doc_id: str, table: dict) -> float:
    """
    Memory strength, Ebbinghaus-style but fed by evidence: a page the model
    opened after it was injected gets stronger; a page injected many times
    and never opened fades. hit_rate.py --strength derives the table from
    the transcripts; this only reads it.
    """
    s = table.get(doc_id)
    if not s:
        return 1.0
    inj, opened = s.get("injected", 0), s.get("opened", 0)
    if opened:
        return min(1.0 + 0.15 * opened, 1.6)
    if inj >= 5:
        return 0.6
    if inj >= 3:
        return 0.8
    return 1.0


def log_decision(session_id: str, prompt: str, terms, picked, rejected, reason: str):
    """One line per decision, injected or not, with why. It is what lets a
    bad injection be explained a week later instead of guessed at."""
    if not session_id:
        return              # evaluation runs (truth.py) pass no session
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        if LOG.exists() and LOG.stat().st_size > MAX_LOG_LINES * 300:
            lines = LOG.read_text(encoding="utf-8").splitlines()[-MAX_LOG_LINES // 2:]
            LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "ts": int(time.time()), "session": session_id[:8], "prompt": prompt[:80],
                "terms": terms, "decision": "inject" if picked else "silent", "reason": reason,
                "picked": [(p["title"][:60], p["score"]) for p in picked],
                "rejected": [(p["title"][:60], p["score"]) for p in rejected[:3]],
            }, ensure_ascii=False) + "\n")
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
        best = ""
        for prefix, weight in imp.items():
            if prefix == "vault" or (doc["id"].startswith(prefix) and len(prefix) > len(best)):
                if prefix != "vault" or not best:
                    w, best = weight, (best if prefix == "vault" else prefix)
    else:
        w = imp.get("transcript", 1.0)
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
    terms, matched, picked = recall(prompt, session_id, cfg)
    if not picked:
        return 0

    L = labels(cfg)
    lines = [f"## {L['recall_header']}", "",
             f"{L['recall_search']}: {', '.join(terms)} — {matched} {L['recall_found']}", ""]
    for r in picked:
        origin = "vault" if r["source"] == "vault" else (r.get("project") or "?")
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
