"""
Shared pieces of the second brain: configuration, paths, language and the few
helpers three or more modules need.

Everything is off until enabled. Installing dupe-guard for the guard should not
start writing conversations to disk; `"brain": {"enabled": true}` in
~/.dupe-guard/config.json (or DUPE_GUARD_BRAIN=1) turns it on.
"""
import json
import os
import unicodedata
from pathlib import Path

HOME_DIR = Path(os.environ.get("DUPE_GUARD_HOME") or (Path.home() / ".dupe-guard"))
CONFIG_FILE = HOME_DIR / "config.json"
DATA = HOME_DIR / "brain"

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
    # folded cwd basename -> note name, for repos whose folder name is not the
    # name of their note
    "project_aliases": {},
    # cwd basenames that mean "no project" (home, scratch). The home folder is
    # always included.
    "no_project_dirs": [],
    # None = automatic: every top-level folder of the vault. Otherwise a list
    # of {"title", "path", "depth", "limit", "inline"}.
    "context_sections": None,
    "distill_model": "sonnet",
    "distill_samples": 3,
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
    cfg["vault"] = Path(os.path.expanduser(cfg["vault"]))
    cfg["transcripts"] = Path(os.path.expanduser(cfg["transcripts"]))
    langs = [l for l in (cfg.get("languages") or ["en"]) if l in LABELS]
    cfg["languages"] = langs or ["en"]
    return cfg


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
        "proposal_session": "session",
        "proposal_word": "proposal",
        "proposal_matched": "matched {ex} and the page was not read",
        "proposal_injections": "recall: {inj} injections, {read} with a page read",
        "applied": "applied",
        "ctx_header": "# SECOND BRAIN (vault) — available in this session",
        "ctx_path": "Path",
        "ctx_content": "Content: {n} .md pages, {kb:.0f} KB (~{ktok} thousand tokens in total).",
        "ctx_dont_load": "**Do NOT load the whole vault.** Use a targeted search — it costs ~0.01s:",
        "ctx_then_read": "Then read only the pages that matched.",
        "ctx_when": "## When to use it",
        "ctx_when_lines": [
            "- **BEFORE** investigating a project or its architecture from scratch: search here first. "
            "Much of the history (decisions, pitfalls, earlier sessions) is already written down.",
            "- When the user says \"remember?\", \"we did this already\", \"how was that\".",
            "- **AT THE END** of sessions with significant work (a new feature, an architectural "
            "decision, a hard bug solved): update the project page, the indexes and any affected hubs. "
            "Use [[wikilinks]], tags in YAML frontmatter, {note_language}, concise — focus on decisions "
            "and pitfalls, not code.",
            "- Do not duplicate a page: update the existing one.",
        ],
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
        "proposal_session": "sessão",
        "proposal_word": "proposta",
        "proposal_matched": "casou {ex} e a pagina nao foi lida",
        "proposal_injections": "recall: {inj} injecoes, {read} com pagina lida",
        "applied": "aplicada",
        "ctx_header": "# SECOND BRAIN (vault Obsidian) — disponivel nesta sessao",
        "ctx_path": "Caminho",
        "ctx_content": "Conteudo: {n} paginas .md, {kb:.0f} KB (~{ktok} mil tokens no total).",
        "ctx_dont_load": "**NAO carregue o vault inteiro.** Use busca dirigida — custa ~0,01s:",
        "ctx_then_read": "Depois leia apenas as paginas que casaram.",
        "ctx_when": "## Quando usar",
        "ctx_when_lines": [
            "- **ANTES** de investigar um projeto/arquitetura do zero: busque aqui primeiro. "
            "Boa parte do contexto historico (decisoes, gotchas, sessoes anteriores) ja esta escrito.",
            "- Quando o usuario disser \"lembra?\", \"ja fizemos isso\", \"como era aquilo\".",
            "- **AO FIM** de sessoes com trabalho significativo (feature nova, decisao "
            "arquitetural, bug complexo resolvido): atualize a pagina do projeto, os indices "
            "e os hubs afetados. Use [[wikilinks]], tags no frontmatter YAML, {note_language}, conciso — "
            "foque em decisoes e gotchas, nao em codigo.",
            "- Nao duplique pagina: atualize a existente.",
        ],
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
