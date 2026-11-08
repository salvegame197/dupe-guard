#!/usr/bin/env python3
"""The catalog: what a page is about, readable without opening it.

Each vault page carries a closed-schema header (frontmatter). A derived
catalog is compiled from the headers alone (first bytes of each file), and
an authority file names the things in the user's life once, with the
everyday words they go by. "what is my car" resolves to an entity of type
vehicle owned by the user, never to a word in a page body.

An OFFLINE tool since 2026-11-08: the reindex still compiles the catalog,
but recall no longer injects a card from it. Measured over 28 days, the card
entered 296 times, always on top, and never about code; the type-noun door
("api", "produto", "cliente") fired on the most ordinary words of a dev
prompt. `--resolve` shows what a prompt would have matched.

    catalog.py --rebuild            compile the catalog from the headers
    catalog.py --backfill [-n N]    write headers for pages that lack one (local model)
    catalog.py --check              validate every header, report violations
    catalog.py --resolve "prompt"   show what a prompt resolves to
    catalog.py --resolve "prompt" --code   the same, in code-session mode
"""
import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brain.core import DATA, fold, is_dataless, load_config, settings  # noqa: E402

SCHEMA = 1
CATALOG = DATA / "catalog.jsonl"
CANDIDATES = DATA / "catalog-candidates.json"
AUTHORITY_NAME = "authority.json"
HEADER_BYTES = 6000

KINDS = ("project", "conversation", "concept", "reference", "log", "hub", "index", "other")
# Entity types and the everyday nouns that name the TYPE, per language. The
# noun belongs to the type, not to the entity: "carro" finds every vehicle,
# and the owner filter picks the user's.
TYPES = {
    "vehicle": {"pt": ["carro", "veiculo", "moto", "automovel"], "en": ["car", "vehicle", "bike"]},
    "watch": {"pt": ["relogio", "smartwatch"], "en": ["watch", "smartwatch"]},
    "camera": {"pt": ["camera", "filmadora"], "en": ["camera"]},
    "phone": {"pt": ["celular", "telefone", "iphone", "smartphone"], "en": ["phone", "smartphone", "iphone"]},
    "computer": {"pt": ["pc", "computador", "notebook", "desktop"], "en": ["pc", "computer", "laptop", "desktop"]},
    "device": {"pt": ["aparelho", "dispositivo", "gadget", "drone"], "en": ["device", "gadget", "drone"]},
    "place": {"pt": ["lugar", "cidade", "bairro", "endereco", "casa"], "en": ["place", "city", "address", "home"]},
    "person": {"pt": ["pessoa", "colega", "cliente"], "en": ["person", "colleague", "client"]},
    "company": {"pt": ["empresa", "firma"], "en": ["company", "firm"]},
    "project": {"pt": ["projeto"], "en": ["project"]},
    "repo": {"pt": ["repositorio", "repo"], "en": ["repository", "repo"]},
    "service": {"pt": ["servico", "api", "microservico"], "en": ["service", "api", "microservice"]},
    "software": {"pt": ["software", "app", "aplicativo", "programa", "ferramenta"], "en": ["software", "app", "tool"]},
    "product": {"pt": ["produto", "compra"], "en": ["product", "purchase"]},
    "topic": {"pt": ["assunto", "tema"], "en": ["topic", "subject"]},
}
PREDICATES = ("owns", "uses", "is_a", "located_in", "works_at", "stack", "status",
              "decided", "price", "when", "part_of", "related", "compared", "sold", "bought")
USER = "user"
GENERIC = ({n for by in TYPES.values() for ns in by.values() for n in ns}
           | {"produto", "product", "item", "coisa", "thing", "user", "usuario", "usuário", "eu", "owner", "me", "i"})
# "device" is where the model puts anything with a plug; the everyday noun
# is more specific. The name usually says which.
REFINE = (("watch", (r"\bwatch\b", r"\brelogio\b", r"\bgarmin\b")),
          ("camera", (r"\bcam", r"insta360", r"gopro", r"osmo", r"\bcamera\b")),
          ("phone", (r"iphone", r"galaxy", r"\bpixel\b", r"celular", r"\bphone\b")),
          ("computer", (r"\bpc\b", r"macbook", r"notebook", r"desktop", r"\blaptop\b", r"\bmac\b")),
          ("vehicle", (r"\bhonda\b", r"\btoyota\b", r"\bfiat\b", r"\bvw\b", r"\bram\b", r"\bmoto\b")))


def refine_type(name: str, etype: str) -> str:
    if etype not in ("device", "product", "other"):
        return etype
    f = fold(name)
    for t, pats in REFINE:
        if any(re.search(pat, f) for pat in pats):
            return t
    return etype

PROMPTS = {
    "pt": """Leia o texto e responda SOMENTE com um JSON neste formato, sem comentario:
{{"kind": "<{kinds}>",
 "entities": [{{"name": "<nome proprio da coisa>", "type": "<{types}>"}}],
 "claims": [["<sujeito>", "<{preds}>", "<objeto>"]],
 "summary": "<uma frase>"}}
Regras: "entities" sao coisas concretas com nome (um carro, um projeto, um lugar, uma pessoa, um aparelho), no maximo 5. O produto ou marca que e o assunto principal do texto e sempre uma entidade.
Quando o texto fala do dono do vault ("eu", "meu"), o sujeito da claim e "user". Maximo 5 claims. Se nao souber, liste vazio.

Titulo: {title}
Texto:
""",
    "en": """Read the text and answer ONLY with a JSON in this shape, no commentary:
{{"kind": "<{kinds}>",
 "entities": [{{"name": "<proper name of the thing>", "type": "<{types}>"}}],
 "claims": [["<subject>", "<{preds}>", "<object>"]],
 "summary": "<one sentence>"}}
Rules: "entities" are concrete named things (a car, a project, a place, a person, a device), at most 5. The product or brand the text is mainly about is always an entity.
When the text is about the vault's owner ("I", "my"), the claim subject is "user". At most 5 claims. Unknown: leave empty.

Title: {title}
Text:
""",
}
MAX_CHARS = 3500


def cfg_settings(cfg):
    return settings(cfg, "catalog", {"enabled": False, "writer": "ollama", "url": "http://localhost:11434",
                                     "model": "qwen2.5:3b", "claude_model": "haiku", "promote_at": 2})


# ---------------------------------------------------------------- frontmatter
def split_header(text: str):
    """(header_lines, body) — header_lines is None when there is no block."""
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end < 0:
        return None, text
    body = text[end + 4:]
    return text[3:end].strip("\n").split("\n"), (body[1:] if body.startswith("\n") else body)


def parse_header(lines):
    """The subset of YAML this module writes and reads: scalars, inline lists,
    block lists. Anything else is kept as an opaque line."""
    out, key = {}, None
    for line in lines or []:
        if re.match(r"^\s+-\s", line) and key:
            out.setdefault(key, []).append(_scalar(line.split("-", 1)[1]))
            continue
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", line)
        if not m:
            key = None
            continue
        key, val = m.group(1), m.group(2).strip()
        if val == "":
            out[key] = []
        elif val.startswith("[") and val.endswith("]"):
            out[key] = [_scalar(x) for x in _split_inline(val[1:-1])]
        else:
            out[key] = _scalar(val)
    return out


def _split_inline(s):
    items, cur, q = [], "", None
    for ch in s:
        if q:
            cur += ch
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch; cur += ch
        elif ch == ",":
            items.append(cur); cur = ""
        else:
            cur += ch
    if cur.strip():
        items.append(cur)
    return items


def _scalar(v):
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    if v in ("true", "false"):
        return v == "true"
    return v


def _quote(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    s = str(v)
    return json.dumps(s, ensure_ascii=False) if re.search(r"[:#\[\]{}|>'\"]|^\s|\s$", s) or s == "" else s


def render_fields(fields: dict) -> list:
    lines = []
    for k, v in fields.items():
        if isinstance(v, list):
            lines.append(f"{k}: [{', '.join(_quote(x) for x in v)}]")
        else:
            lines.append(f"{k}: {_quote(v)}")
    return lines


OUR_KEYS = ("schema", "kind", "entities", "claims", "title_assigned", "summary")


def write_header(text: str, fields: dict) -> str:
    """The page with our fields set, other header lines untouched."""
    lines, body = split_header(text)
    kept = []
    skip_block = False
    for line in (lines or []):
        m = re.match(r"^([A-Za-z_][\w-]*):", line)
        if m:
            skip_block = m.group(1) in OUR_KEYS
        elif not re.match(r"^\s+-\s", line):
            skip_block = False
        if not skip_block:
            kept.append(line)
    new = "---\n" + "\n".join(kept + render_fields(fields)) + "\n---\n"
    return new + (body if lines is not None else ("\n" + text if not text.startswith("\n") else text))


# ---------------------------------------------------------------- validation
def validate(fields: dict, authority: dict):
    """(clean_fields, problems). Whatever is outside the schema is dropped
    and reported; nothing outside the lists reaches the catalog."""
    clean, problems = {}, []
    if fields.get("schema") not in (SCHEMA, str(SCHEMA)):
        problems.append("schema")
    clean["schema"] = SCHEMA
    kind = fields.get("kind")
    if kind in KINDS:
        clean["kind"] = kind
    else:
        problems.append(f"kind:{kind}")
        clean["kind"] = "other"
    ents = []
    for e in fields.get("entities") or []:
        if e in authority:
            ents.append(e)
        else:
            problems.append(f"entity:{e}")
    clean["entities"] = ents
    claims = []
    for c in fields.get("claims") or []:
        parts = [p.strip() for p in str(c).split("|")]
        if len(parts) == 3 and parts[1] in PREDICATES and all(parts):
            claims.append("|".join(parts))
        else:
            problems.append(f"claim:{c}")
    clean["claims"] = claims
    clean["title_assigned"] = bool(fields.get("title_assigned", False))
    summary = str(fields.get("summary") or "").strip()
    clean["summary"] = summary[:200]
    return clean, problems


# ---------------------------------------------------------------- authority
def authority_path(cfg) -> Path:
    return Path(cfg["vault"]) / AUTHORITY_NAME


class AuthorityUnavailable(OSError):
    """The authority file is a cloud placeholder: reading it would block, and
    treating it as empty would let the next save wipe every entity."""


def load_authority(cfg, writer: bool = False) -> dict:
    """The authority by id. A reader gets {} when the file is evicted from
    disk (and a line on stderr); a `writer` (backfill, relink) gets
    AuthorityUnavailable instead, because it would save over the real file."""
    path = authority_path(cfg)
    if is_dataless(path):
        if writer:
            raise AuthorityUnavailable(f"{path} is not on disk (cloud placeholder); "
                                       f"run: brctl download \"{path}\"")
        print(f"catalog: {path.name} not on disk (cloud placeholder), authority treated as empty",
              file=sys.stderr)
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {e["id"]: e for e in data.get("entities", []) if e.get("id")}
    except (OSError, ValueError, TypeError, AttributeError):
        return {}


def save_authority(cfg, entities: dict):
    rows = sorted(entities.values(), key=lambda e: e["id"])
    authority_path(cfg).write_text(json.dumps({"schema": SCHEMA, "entities": rows},
                                              ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", fold(name)).strip("-")
    return s[:60]


def type_nouns(lang_list):
    """noun -> type for the configured languages."""
    out = {}
    for t, by_lang in TYPES.items():
        for lang in lang_list:
            for n in by_lang.get(lang, []):
                out[n] = t
    return out


# ---------------------------------------------------------------- catalog
def vault_pages(cfg):
    root = Path(cfg["vault"])
    for p in sorted(root.rglob("*.md")):
        rel = str(p.relative_to(root))
        if any(part.startswith(".") for part in p.parts):
            continue
        yield rel, p


def read_header_only(path: Path):
    """The header, reading only the first bytes: the catalog never opens a
    page beyond its header."""
    if is_dataless(path):
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(HEADER_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return None
    lines, _ = split_header(head)
    return parse_header(lines) if lines is not None else None


def rebuild(cfg):
    """Compile the catalog from headers. Returns (rows, problems by page)."""
    authority = load_authority(cfg)
    rows, problems, skipped = [], {}, 0
    for rel, p in vault_pages(cfg):
        h = read_header_only(p)
        if h is None:
            skipped += 1
            continue
        if "schema" not in h:
            continue
        clean, probs = validate(h, authority)
        if probs:
            problems[rel] = probs
        rows.append({"id": rel, "title": Path(rel).stem, "date": str(h.get("date") or ""), **clean})
    CATALOG.parent.mkdir(parents=True, exist_ok=True)
    with open(CATALOG, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return rows, problems, skipped


def load_catalog():
    try:
        return [json.loads(l) for l in CATALOG.read_text(encoding="utf-8").splitlines() if l.strip()]
    except (OSError, ValueError):
        return []


# ---------------------------------------------------------------- resolve
POSSESSIVE = {"pt": ("meu", "minha", "meus", "minhas", "tenho", "comprei"), "en": ("my", "mine", "i have", "i bought")}


def resolve(prompt: str, cfg: dict, authority=None, catalog=None, code_session: bool = False):
    """[(entity, pages, claims)] for a prompt, by authority first.

    Two doors: a variant or canonical name in the prompt names the entity
    outright; an everyday noun of a TYPE ("carro") plus a possessive picks
    the user's entities of that type, or all of that type when none is
    owned. No score, no threshold: it matches or it does not.

    In a `code_session` the type door only opens with a possessive: "meu
    carro" still resolves, a bare "api" or "cliente" in a prompt about code
    does not (those two words alone brought Decolar and a wine)."""
    authority = load_authority(cfg) if authority is None else authority
    catalog = load_catalog() if catalog is None else catalog
    if not authority:
        return []
    text = " " + re.sub(r"[^\w\s-]", " ", fold(prompt)) + " "
    words = set(re.findall(r"[a-z0-9][\w-]*", text))
    hits = []
    for e in authority.values():
        names = [e.get("canonical", "")] + list(e.get("variants") or [])
        if any(n and (" " + fold(n) + " ") in text for n in names):
            hits.append(e["id"])
    nouns = type_nouns(cfg["languages"])
    asked_types = {nouns[w] for w in words if w in nouns}
    possessive = any(p in text for lang in cfg["languages"] for p in POSSESSIVE.get(lang, ()))
    if asked_types and (possessive or not code_session):
        for t in asked_types:
            of_type = [e for e in authority.values() if e.get("type") == t]
            mine = [e for e in of_type if e.get("owner") == USER]
            # "my watch" and "the watch I sold" both mean the user's, when
            # there is one; only without any owned thing does the type's
            # whole shelf come, and then only when asked possessively.
            chosen = mine or (of_type if possessive else [])
            hits += [e["id"] for e in chosen[:3]]
    seen, out = set(), []
    by_entity = defaultdict(list)
    for row in catalog:
        for eid in row.get("entities") or []:
            by_entity[eid].append(row)
    for eid in hits:
        if eid in seen:
            continue
        seen.add(eid)
        e = authority[eid]
        pages = by_entity.get(eid) or [{"id": p, "title": Path(p).stem, "claims": [], "summary": ""}
                                        for p in e.get("pages") or []]
        # The page NAMED after the thing is its home; otherwise the one that
        # says the most about it. One card, one page: the keyword index
        # keeps its say on the rest.
        canon = fold(e.get("canonical", ""))
        pages = sorted(pages, key=lambda r: (fold(r["title"]) != canon,
                                             -sum(1 for c in r.get("claims") or [] if eid in c),
                                             r.get("kind") == "conversation"))
        claims = [c for row in pages[:1] for c in row.get("claims") or [] if eid in c or e["canonical"] in c]
        out.append((e, pages[:1], claims))
    return out


# ---------------------------------------------------------------- backfill
def kind_from_path(rel: str) -> str:
    """The folder says what the page is when the model does not."""
    for prefix, kind in (("Development/Projects/", "project"), ("Claude Web/", "conversation"),
                         ("Development/Concepts/", "concept"), ("Hubs/", "hub"), ("Sessions/", "log")):
        if rel.startswith(prefix):
            return kind
    return "other"


def ask_model(title: str, text: str, cfg: dict):
    """The header proposal for a page, from the configured writer: the local
    model (free, uneven) or a Claude model through `claude -p` (costs quota,
    abstracts and classifies reliably). Same prompt, same validation after."""
    s = cfg_settings(cfg)
    lang = cfg["languages"][0] if cfg["languages"][0] in PROMPTS else "en"
    prompt = PROMPTS[lang].format(kinds="|".join(KINDS), types="|".join(TYPES), preds="|".join(PREDICATES),
                                  title=title) + text[:MAX_CHARS]
    if s.get("writer") == "claude":
        from brain import distill
        from brain.core import worker_dir
        try:
            # Same guarded child as the distiller: binary from config, no
            # tools, no hooks, its own working folder. The page text goes in
            # as data: a sentence in a note must not become a command.
            fenced = (PROMPTS[lang].format(kinds="|".join(KINDS), types="|".join(TYPES),
                                           preds="|".join(PREDICATES), title=title)
                      + distill.as_data(text[:MAX_CHARS]))
            env = distill.child_env()
            r = subprocess.run(distill.child_command(s.get("claude_model", "haiku"), cfg, env),
                               input=fenced, cwd=str(worker_dir()),
                               capture_output=True, text=True, timeout=240, env=env)
            m = re.search(r"\{.*\}", r.stdout or "", re.S)
            return json.loads(m.group(0)) if m else None
        except (subprocess.SubprocessError, OSError, ValueError):
            return None
    req = urllib.request.Request(f"{s['url'].rstrip('/')}/api/generate",
                                 data=json.dumps({"model": s["model"], "prompt": prompt, "stream": False,
                                                  "format": "json",
                                                  "options": {"temperature": 0.1, "num_predict": 300},
                                                  "keep_alive": "30m"}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=240) as r:
            out = json.loads(r.read())["response"]
        m = re.search(r"\{.*\}", out, re.S)
        return json.loads(m.group(0)) if m else None
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None


def entity_id(name: str, etype: str, authority: dict):
    """The authority entry for a name, or None. Match on canonical or a
    variant, folded; type must agree."""
    f = fold(name)
    same_type, any_type = None, None
    for e in authority.values():
        names = {fold(e.get("canonical", ""))} | {fold(v) for v in e.get("variants") or []}
        if f in names or (e.get("type") == etype and any(same_thing(f, n) for n in names)):
            if e.get("type") == etype:
                same_type = e["id"]
            any_type = any_type or e["id"]
    # The name identifies the thing; the type only describes it. Two runs
    # naming "Northwind" a project and a product must not make two cards.
    return same_type or any_type


def same_thing(a: str, b: str) -> bool:
    """"fiat argo", "fiat argo 2024 trekking", "argo 2024" and "fiat argo
    1.5" are one car: the shorter folded name sits whole inside the longer
    at word boundaries, and has at least two words (so "iphone" does not
    swallow every iPhone ever mentioned)."""
    if not a or not b or a == b:
        return a == b
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    return len(short.split()) >= 2 and (" " + short + " ") in (" " + long_ + " ")


def load_candidates():
    try:
        return json.loads(CANDIDATES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_candidates(c):
    CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
    CANDIDATES.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")


def backfill(cfg, limit=0, prefix="", force=False, log=print, workers=1):
    """Write a header for every page that has none. Entities the model names
    become candidates; a candidate seen on `promote_at` pages (or owned by
    the user) enters the authority. Pages are rewritten in place."""
    s = cfg_settings(cfg)
    authority = load_authority(cfg, writer=True)
    candidates = load_candidates()
    done = failed = 0
    t0 = time.time()
    todo = []
    for rel, p in vault_pages(cfg):
        if not rel.startswith(prefix) or is_dataless(p):
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        lines, body = split_header(text)
        existing = parse_header(lines) if lines is not None else {}
        if existing.get("schema") and not force:
            continue
        todo.append((rel, p, text, body))
        if limit and len(todo) >= limit:
            break
    # The model calls run in parallel; the authority and the pages are
    # written by this thread only, in order, so two pages naming the same
    # new thing cannot race into two cards.
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    answers = [pool.submit(ask_model, Path(rel).stem, body.strip(), cfg) for rel, p, text, body in todo]
    for (rel, p, text, body), fut in zip(todo, answers):
        ans = fut.result()
        if not isinstance(ans, dict):
            failed += 1
            continue
        fields = {"schema": SCHEMA, "kind": ans.get("kind") if ans.get("kind") in KINDS else kind_from_path(rel),
                  "entities": [], "claims": [],
                  "title_assigned": rel.startswith("Claude Web/"), "summary": str(ans.get("summary") or "")}
        names = {}
        for e in ans.get("entities") or []:
            if not isinstance(e, dict) or not e.get("name") or e.get("type") not in TYPES:
                continue
            name, etype = str(e["name"]).strip()[:80], e["type"]
            if fold(name) in GENERIC or len(fold(name)) < 3:
                continue                  # "carro" is a type, not a thing
            etype = refine_type(name, etype)
            eid = entity_id(name, etype, authority)
            if eid is None:
                key = f"{etype}:{slug(name)}"
                c = candidates.setdefault(key, {"name": name, "type": etype, "pages": [], "owned": False})
                if rel not in c["pages"]:
                    c["pages"].append(rel)
                names[fold(name)] = (key, None)
            else:
                names[fold(name)] = (None, eid)
                fields["entities"].append(eid)
        raw_claims = []
        for c in ans.get("claims") or []:
            if not (isinstance(c, list) and len(c) == 3):
                continue
            sub, pred, obj = (str(x.get("name", "") if isinstance(x, dict) else x).strip() for x in c)
            if pred not in PREDICATES or not sub or not obj:
                continue
            if fold(sub) in ("eu", "owner", "i", "me", "user", "usuario"):
                sub = USER
            raw_claims.append((sub, pred, obj))
            key = names.get(fold(obj), (None, None))[0]
            if key and sub == USER and pred in ("owns", "bought"):
                candidates[key]["owned"] = True
        # Promotion: enough pages, or owned by the user (a thing of theirs is
        # worth a card on first sight). Done BEFORE the claims are written so
        # they carry the id, not the name.
        for key, c in list(candidates.items()):
            if len(c["pages"]) >= s["promote_at"] or c["owned"]:
                eid = f"{c['type']}-{slug(c['name'])}"
                if eid not in authority:
                    authority[eid] = {"id": eid, "type": c["type"], "canonical": c["name"], "variants": [],
                                      "owner": USER if c["owned"] else None, "pages": list(c["pages"])}
                else:
                    for pg in c["pages"]:
                        if pg not in authority[eid]["pages"]:
                            authority[eid]["pages"].append(pg)
                    if c["owned"]:
                        authority[eid]["owner"] = USER
                if fold(c["name"]) in names:
                    names[fold(c["name"])] = (None, eid)
                    if eid not in fields["entities"]:
                        fields["entities"].append(eid)
                del candidates[key]
        for sub, pred, obj in raw_claims:
            sub_id = names.get(fold(sub), (None, None))[1]
            obj_id = names.get(fold(obj), (None, None))[1]
            fields["claims"].append("|".join([sub_id or sub, pred, obj_id or obj])[:200])
        clean, _ = validate(fields, authority)
        p.write_text(write_header(text, clean), encoding="utf-8")
        done += 1
        if done % 25 == 0:
            rate = (time.time() - t0) / done
            log(f"  {done}/{len(todo)}  {rate:.1f}s per page, ~{rate * (len(todo) - done) / 60:.0f} min left")
            save_authority(cfg, authority); save_candidates(candidates)
    save_authority(cfg, authority); save_candidates(candidates)
    log(f"headers written for {done} page(s), {failed} failed, {len(authority)} entities in authority, "
        f"{len(candidates)} candidates, {(time.time() - t0) / 60:.1f} min")
    return done, failed


# ---------------------------------------------------------------- relink
def merge_cards(authority: dict) -> int:
    """Two cards of one type whose names (canonical or variant) are the same
    thing become one: the longer name keeps the card, the rest are kept as
    variants, pages and ownership are joined. Returns the number merged."""
    merged = 0
    for eid in sorted(list(authority), key=lambda i: -len(authority[i].get("canonical", ""))):
        if eid not in authority:
            continue
        e = authority[eid]
        for oid in list(authority):
            if oid == eid or oid not in authority or authority[oid].get("type") != e.get("type"):
                continue
            o = authority[oid]
            mine = [fold(e.get("canonical", ""))] + [fold(v) for v in e.get("variants") or []]
            theirs = [fold(o.get("canonical", ""))] + [fold(v) for v in o.get("variants") or []]
            if any(same_thing(a, b) for a in mine for b in theirs if a and b):
                e["variants"] = sorted(set(e.get("variants") or []) | {o.get("canonical", "")} | set(o.get("variants") or []))
                e["pages"] = list(dict.fromkeys((e.get("pages") or []) + (o.get("pages") or [])))
                e["owner"] = e.get("owner") or o.get("owner")
                del authority[oid]
                merged += 1
    return merged


def relink(cfg, log=print):
    """Point every header at the authority as it is now. Cards get merged
    and renamed after pages were written; an id nobody has any more is
    matched by its slug against the variants, and "user" is never an
    entity, only a subject."""
    authority = load_authority(cfg, writer=True)
    merge_cards(authority)
    by_slug = {}
    for e in authority.values():
        for name in [e.get("canonical", "")] + list(e.get("variants") or []):
            by_slug.setdefault(slug(name), e["id"])
    user_ids = {f"{t}-user" for t in TYPES} | {"person-owner", "user"}

    def map_id(eid):
        if eid in authority:
            return eid
        if eid in user_ids:
            return USER
        _, _, rest = eid.partition("-")
        return by_slug.get(rest)

    changed = 0
    for rel, p in vault_pages(cfg):
        h = read_header_only(p)
        if not h or "schema" not in h:
            continue
        ents, claims, touched = [], [], False
        for eid in h.get("entities") or []:
            new = map_id(eid)
            if new != eid:
                touched = True
            if new and new != USER and new not in ents:
                ents.append(new)
        for c in h.get("claims") or []:
            parts = str(c).split("|")
            if len(parts) == 3:
                sub, obj = map_id(parts[0]) or parts[0], map_id(parts[2]) or parts[2]
                if (sub, obj) != (parts[0], parts[2]):
                    touched = True
                c = "|".join([sub, parts[1], obj])
            claims.append(c)
        if touched:
            fields = {**{k: h.get(k) for k in OUR_KEYS}, "entities": ents, "claims": claims}
            clean, _ = validate(fields, authority)
            p.write_text(write_header(p.read_text(encoding="utf-8", errors="ignore"), clean), encoding="utf-8")
            changed += 1
    # Pages listed on a card must carry the card, and vice versa.
    for e in authority.values():
        e["pages"] = [pg for pg in e.get("pages") or [] if (Path(cfg["vault"]) / pg).exists()]
    save_authority(cfg, authority)
    log(f"relinked {changed} page(s)")
    return changed


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--resolve", metavar="PROMPT")
    ap.add_argument("--code", action="store_true", help="resolve as in a code session (type nouns need a possessive)")
    ap.add_argument("--relink", action="store_true", help="re-point headers at merged/renamed cards")
    ap.add_argument("-n", "--limit", type=int, default=0)
    ap.add_argument("--prefix", default="")
    ap.add_argument("--force", action="store_true", help="rewrite headers that exist")
    ap.add_argument("--workers", type=int, default=1, help="parallel model calls")
    a = ap.parse_args()
    cfg = load_config()
    try:
        if a.backfill:
            backfill(cfg, a.limit, a.prefix, a.force, workers=a.workers)
            rows, problems, skipped = rebuild(cfg)
            print(f"catalog: {len(rows)} page(s), {len(problems)} with problems, {skipped} without any header")
        if a.relink:
            relink(cfg)
            rows, problems, skipped = rebuild(cfg)
            print(f"catalog: {len(rows)} page(s), {len(problems)} with problems")
    except AuthorityUnavailable as e:
        print(f"catalog: aborted, {e}", file=sys.stderr)
        return 1
    if a.rebuild or a.check:
        rows, problems, skipped = rebuild(cfg)
        print(f"catalog: {len(rows)} page(s) with a header, {len(problems)} with problems, {skipped} without any header")
        if a.check:
            for rel, probs in sorted(problems.items()):
                print(f"  {rel}: {', '.join(probs)}")
    if a.resolve:
        for e, pages, claims in resolve(a.resolve, cfg, code_session=a.code):
            print(f"{e['id']} ({e['type']}, owner={e.get('owner')}) -> {[p['id'] for p in pages]}")
            for c in claims:
                print(f"    {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
