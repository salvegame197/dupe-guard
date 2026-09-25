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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, fold, hook_output, labels, load_config   # noqa: E402

STATE_DIR = DATA / "recall-state"
MANUAL_STOPWORDS = DATA / "recall-stopwords.json"

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


def title_hit(terms, title: str):
    """
    A term matching the title — by WORD, never by substring.

    'start' inside 'starter' is not a shared subject, it is a letter
    collision. The title is split into words and a term must match one whole,
    tolerating only a short plural inflection (up to 2 characters).
    """
    words = set(re.findall(r"[a-z0-9]{3,}", fold(title)))
    for t in terms:
        if len(t) < MIN_TITLE_TERM:
            continue
        for w in words:
            if w == t:
                return t
            short, long_ = (t, w) if len(t) <= len(w) else (w, t)
            if len(long_) - len(short) <= 2 and long_.startswith(short):
                return t
    return None


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


def pick(results, terms, seen, session_id):
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
        if not title_hit(terms, r["title"] or ""):
            continue
        picked.append(r)
        if len(picked) >= MAX_RESULTS:
            break
    return picked


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    cfg = load_config()
    if not cfg["enabled"]:
        return 0

    from brain import ksearch
    prompt = payload.get("prompt") or ""
    session_id = payload.get("session_id") or ""
    if len(prompt.strip()) < MIN_PROMPT_CHARS or not ksearch.INDEX.exists():
        return 0

    terms = terms_from(prompt, cfg["languages"])
    # One term alone is usually too generic, unless it is long and specific.
    if len(terms) < 2 and not (terms and len(terms[0]) >= MIN_SOLO_TERM):
        return 0

    found, _ = ksearch.search(terms, limit=8)
    results = [{"score": round(s, 1), "source": d["src"], "title": d["title"],
                "project": d["project"], "date": d["date"], "id": d["id"],
                "snippets": [ksearch.snippet(t["t"], terms) for t in b]}
               for s, d, b in found[:8]]
    seen = already_seen(session_id)
    picked = pick(results, terms, seen, session_id)
    if not picked:
        return 0

    L = labels(cfg)
    lines = [f"## {L['recall_header']}", "",
             f"{L['recall_search']}: {', '.join(terms)} — {len(found)} {L['recall_found']}", ""]
    for r in picked:
        origin = "vault" if r["source"] == "vault" else (r.get("project") or "?")
        when = f", {r['date']}" if r.get("date") else ""
        lines.append(f"**{r['title']}** ({origin}{when})")
        for s in r.get("snippets", [])[:2]:
            lines.append(f"  - {s}")
        lines.append("")
    lines.append(L["recall_footer"] + f" `python3 {Path(__file__).parent / 'ksearch.py'} <terms>`")

    remember(session_id, seen | {r["id"] for r in picked})
    print(hook_output("UserPromptSubmit", "\n".join(lines)))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
