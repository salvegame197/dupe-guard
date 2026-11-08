"""
Shared pieces of the second brain: configuration, paths, language and the few
helpers three or more modules need.

Everything is off until enabled. Installing dupe-guard for the guard should not
start writing conversations to disk; `"brain": {"enabled": true}` in
~/.dupe-guard/config.json (or DUPE_GUARD_BRAIN=1) turns it on.
"""
import json
import os
import re
import unicodedata
from pathlib import Path

HOME_DIR = Path(os.environ.get("DUPE_GUARD_HOME") or (Path.home() / ".dupe-guard"))
CONFIG_FILE = HOME_DIR / "config.json"
DATA = HOME_DIR / "brain"
# Working directory of every `claude -p` the brain starts (distill, catalog).
# A fixed folder of its own, so the child's transcripts land in one known
# project folder under ~/.claude/projects that the index can leave out,
# instead of in the folder of whatever session happened to stop.
WORKER_DIR = HOME_DIR / "worker"


def worker_dir() -> Path:
    WORKER_DIR.mkdir(parents=True, exist_ok=True)
    return WORKER_DIR


def project_folder_name(cwd) -> str:
    """The folder name Claude Code gives a working directory under
    ~/.claude/projects: every character outside [A-Za-z0-9] becomes '-'
    ("/Users/x/.dupe-guard/worker" -> "-Users-x--dupe-guard-worker")."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))

DEFAULTS = {
    "enabled": False,
    # Outside HOME_DIR on purpose: that folder is index, cache and state,
    # safe to delete; the vault is the user's notes.
    "vault": str(Path.home() / "second-brain"),
    "transcripts": str(Path.home() / ".claude" / "projects"),
    # First language drives the labels written to the vault; all of them feed
    # the recall stopword lists and the correction phrases proposals look for.
    "languages": ["en"],
    "note_language": "English",
    "raw_dir": "Sessions/Raw",
    "proposals_page": "Harness Proposals.md",
    # a markdown table of questions with known answers; see brain/truth.py
    "truth_page": "Known Truths.md",
    # folded cwd basename -> note name, for repos whose folder name is not the
    # name of their note
    "project_aliases": {},
    # cwd basenames that mean "no project" (home, scratch). The home folder is
    # always included.
    "no_project_dirs": [],
    # None = automatic: every top-level folder of the vault. Otherwise a list
    # of {"title", "path", "depth", "limit", "inline"}.
    "context_sections": None,
    # Importance by origin (Generative Agents weigh relevance, recency and
    # importance; a keyword score alone has only the first). Keys are "vault"
    # folder prefixes or "transcript"; a note distilled by hand outranks a raw
    # session, and a project note outranks a chat log. Multipliers on the score.
    # "memory" is the native memory Claude Code writes per project folder
    # (~/.claude/projects/*/memory/*.md): a rule the person taught, so it
    # outranks any vault folder; a `feedback` note (a correction) gets
    # "memory_feedback" on top of that.
    "importance": {"vault": 1.2, "transcript": 1.0, "memory": 1.6, "memory_feedback": 1.25},
    # Recent pages get a small boost: 1.15 within a week, 1.08 within a month.
    "recency_boost": True,
    # Vault folders whose pages may be recalled on a strong BODY match even
    # when no search term is in the title. The title filter exists to keep
    # chat logs from matching on a stray word; curated project notes hold the
    # answer in a section, under a title that names the project, not the
    # question. Empty = title match required everywhere.
    "trusted_prefixes": [],
    "body_match_min": 14.0,
    # Vault folder prefixes that recall never injects in a CODE session (cwd
    # inside a git repo, or a repo edited this session). Chat exports are
    # about the person's life; in a session about code they only match by
    # accident, and they were 59 % of what was injected. Empty = mute none.
    "code_session_mute": ["Claude Web/"],
    "distill_model": "sonnet",
    "distill_samples": 3,
    # Automatic distilling. Off by default: it spends the user's plan. When
    # on, the Stop hook starts a detached worker once enough sessions have
    # piled up (or the oldest waited a day), which distils up to a batch,
    # commits one note at a time, and leaves a report for the next session.
    "distill_auto": False,
    "distill_batch": 5,
    "distill_min_pending": 3,
    "distill_max_age_hours": 24,
    # Path to the claude binary; found on PATH or in the usual places if unset.
    "claude_bin": None,
    # Local embeddings (brain/embed.py). Off by default; needs an Ollama
    # server with the model pulled. Keyword search stays the base; the vector
    # side adds `weight` x (cos - min_cos) to a document's score and can bring
    # in documents keywords missed.
    # Questions written ahead of time for every block by a small local model
    # (brain/questions.py); indexed as text, so paraphrase is matched by the
    # keyword search itself. Off by default; a long background job.
    "questions": {"enabled": False, "url": "http://localhost:11434", "model": "qwen2.5:3b",
                  "per_unit": 3, "sources": ["vault"]},
    # Entity catalog compiled from page headers (brain/catalog.py).
    "catalog": {"enabled": False, "url": "http://localhost:11434", "model": "qwen2.5:3b", "promote_at": 2},
    "embeddings": {"enabled": False, "url": "http://localhost:11434", "model": "bge-m3",
                   "weight": 30.0, "min_cos": 0.45, "bonus_cos": 0.55, "strong_cos": 0.6, "gap": 0.03, "top": 20},
}


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        cfg.update((json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}).get("brain") or {})
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    if os.environ.get("DUPE_GUARD_BRAIN") == "1":
        cfg["enabled"] = True
    if os.environ.get("DUPE_GUARD_VAULT"):
        cfg["vault"] = os.environ["DUPE_GUARD_VAULT"]
    if os.environ.get("DUPE_GUARD_TRANSCRIPTS"):
        cfg["transcripts"] = os.environ["DUPE_GUARD_TRANSCRIPTS"]
    # Set by the distill worker for the `claude -p` it starts: that child
    # session must not run the live hooks (it used to inject recall into its
    # own prompt and write into the decision log).
    if os.environ.get("DUPE_GUARD_DISABLE") == "1":
        cfg["enabled"] = False
    cfg["vault"] = Path(os.path.expanduser(cfg["vault"]))
    cfg["transcripts"] = Path(os.path.expanduser(cfg["transcripts"]))
    langs = [l for l in (cfg.get("languages") or ["en"]) if l in LABELS]
    cfg["languages"] = langs or ["en"]
    return cfg


def is_dataless(path) -> bool:
    """True when the file is a cloud placeholder whose bytes are not on disk
    (iCloud "Optimize Mac Storage" evicts files when the disk fills). Reading
    one blocks until the cloud delivers it, which in a hook means a hang.
    macOS marks them with UF_DATALESS in st_flags; elsewhere always False."""
    try:
        return bool(os.stat(path).st_flags & 0x40000000)
    except (OSError, AttributeError):
        return False


REDACTED = "[REDACTED]"

# What a secret pasted into a prompt looks like. Each pattern is replaced
# before the text is written anywhere durable (vault, queue, index, log).
# Prefixed keys (sk-, ghp_, xox, AKIA) are vendor formats; the JWT is three
# base64url parts; `password=` keeps the key and drops the value; the last
# one is any long base64 run (certificates, encoded blobs).
_SECRET_PATTERNS = [
    re.compile(r"\b(?:sk|pk|rk)-(?:[a-z]+-)?[A-Za-z0-9_-]{16,}"),            # OpenAI/Anthropic/Stripe style
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),                           # Slack
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                                     # AWS access key id
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}"),  # JWT
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
]
_SECRET_KV = re.compile(
    r"(?i)\b(senha|password|passwd|pwd|token|secret|client_secret|api[_-]?key|"
    r"access[_-]?key|private[_-]?key|auth)\s*([:=])\s*[\"']?([^\s\"',;]{4,})")
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")


def _looks_encoded(s: str) -> bool:
    """A base64 run and not a long word: has a digit, upper and lower case."""
    return any(c.isdigit() for c in s) and any(c.isupper() for c in s) and any(c.islower() for c in s)


def redact(text: str) -> str:
    """Text with anything that looks like a secret replaced by [REDACTED].
    Applied to prompts before they reach the vault, the distill queue, the
    search index or the decision log. It is a filter, not a detector: a
    false positive costs one unreadable token, a miss costs a leaked key."""
    if not text:
        return text
    for rx in _SECRET_PATTERNS:
        text = rx.sub(REDACTED, text)
    text = _SECRET_KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)
    text = _BASE64_RUN.sub(lambda m: REDACTED if _looks_encoded(m.group(0)) else m.group(0), text)
    return text


def settings(cfg: dict, key: str, defaults: dict) -> dict:
    """A config section with its defaults filled in."""
    return dict(defaults, **(cfg.get(key) or {}))


def fold(s: str) -> str:
    """Lowercase without accents: 'Pagamento' and 'pagamento' match 'pagamento'."""
    s = unicodedata.normalize("NFD", (s or "").lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def hook_output(event: str, text: str) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event,
                                              "additionalContext": text}})


def injected_context(attachment: dict) -> str:
    """
    The text a hook injected, from a transcript attachment. Claude Code records
    it two ways: hook_success (stdout with the whole JSON) or
    hook_additional_context (just the text, in a list). Reading only one of
    them makes a working hook look silent.
    """
    if attachment.get("type") == "hook_additional_context":
        c = attachment.get("content")
        if isinstance(c, list):
            return "\n".join(x for x in c if isinstance(x, str))
        return c if isinstance(c, str) else ""
    if attachment.get("type") == "hook_success":
        try:
            return json.loads(attachment.get("stdout") or "")["hookSpecificOutput"]["additionalContext"]
        except (ValueError, KeyError, TypeError):
            return ""
    return ""


# ------------------------------------------------------------------ language

LABELS = {
    "en": {
        "sessions_of": "Sessions of",
        "no_note": "⚠️ no note in the vault",
        "subject": "Subject",
        "note": "Note",
        "repo": "Repo",
        "ended_by": "Ended by",
        "requests": "Requests",
        "more_requests": "requests",
        "files_touched": "Files touched",
        "commits": "Commits",
        "proposals": "Harness proposals",
        "see": "see",
        "transcript_expires": "expires with the local transcript retention",
        "raw_tags": "[sessions, raw, claude-code]",
        "recall_header": "Accumulated knowledge on this subject",
        "recall_search": "Automatic search for",
        "recall_found": "result(s), the strongest below.",
        "recall_footer": ("_A reminder of work done before; it may be out of date. "
                          "Check against the code or the vault before acting on it._"),
        "recall_memory_of": "memory of {p}",
        "distill_skipped": "Distill skipped: this session already wrote {files}",
        "proposal_session": "session",
        "proposal_word": "proposal",
        "proposal_injections": "recall: {inj} injections, {read} with a page read",
        "applied": "applied",
        "superseded_on": "Superseded on",
        "no_longer_exists": "no longer exists in the repository (checked automatically)",
        "rule_retired": "rule retired, its counter reached zero (misled more than it helped)",
        "conventions": "Convention votes", "retired": "retired",
        "ctx_header": "# SECOND BRAIN (vault) — available in this session",
        "ctx_path": "Path",
        "ctx_content": "Content: {n} .md pages, {kb:.0f} KB (~{ktok} thousand tokens in total).",
        "ctx_dont_load": "**Do NOT load the whole vault.** Use a targeted search — it costs ~0.01s:",
        "ctx_then_read": "Then read only the pages that matched.",
        "ctx_failed_title": "## ⚠️ Automatic session recording failed recently",
        "ctx_failed_body": "See `{path}` — until it is fixed, sessions may not be becoming notes "
                           "(local transcripts expire).",
    },
    "pt": {
        "sessions_of": "Sessoes de",
        "no_note": "⚠️ sem nota no vault",
        "subject": "Assunto",
        "note": "Nota",
        "repo": "Repo",
        "ended_by": "Encerrou por",
        "requests": "Pedidos",
        "more_requests": "pedidos",
        "files_touched": "Arquivos tocados",
        "commits": "Commits",
        "proposals": "Propostas do harness",
        "see": "ver",
        "transcript_expires": "expira com a retencao local de transcripts",
        "raw_tags": "[sessoes, raw, claude-code]",
        "recall_header": "Conhecimento acumulado sobre este assunto",
        "recall_search": "Busca automatica por",
        "recall_found": "resultado(s), os mais fortes abaixo.",
        "recall_footer": ("_Isto e um lembrete do que ja foi trabalhado antes — pode estar "
                          "desatualizado. Confirme no codigo/vault antes de agir._"),
        "recall_memory_of": "memoria de {p}",
        "distill_skipped": "Distill pulado: esta sessao ja escreveu {files}",
        "proposal_session": "sessão",
        "proposal_word": "proposta",
        "proposal_injections": "recall: {inj} injecoes, {read} com pagina lida",
        "applied": "aplicada",
        "superseded_on": "Superado em",
        "no_longer_exists": "não existe mais no repositório (conferido automaticamente)",
        "rule_retired": "regra aposentada, o contador chegou a zero (atrapalhou mais do que ajudou)",
        "conventions": "Votos nas convencoes", "retired": "aposentada(s)",
        "ctx_header": "# SECOND BRAIN (vault Obsidian) — disponivel nesta sessao",
        "ctx_path": "Caminho",
        "ctx_content": "Conteudo: {n} paginas .md, {kb:.0f} KB (~{ktok} mil tokens no total).",
        "ctx_dont_load": "**NAO carregue o vault inteiro.** Use busca dirigida — custa ~0,01s:",
        "ctx_then_read": "Depois leia apenas as paginas que casaram.",
        "ctx_failed_title": "## ⚠️ A gravacao automatica de sessao falhou recentemente",
        "ctx_failed_body": "Ver `{path}` — enquanto isso, sessoes podem nao estar "
                           "virando nota (o transcript local expira).",
    },
}

# Every language's recall header, so the readers recognise injections made
# under any configuration.
RECALL_MARKS = tuple(l["recall_header"] for l in LABELS.values())


def labels(cfg: dict) -> dict:
    return LABELS[cfg["languages"][0]]
