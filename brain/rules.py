"""
rules — the "Rules in force" section at the top of a project note.

A project note is narrative: dated sections, hundreds of them in the big
ones (224 KB for the main repo). The model and the person read the first
screen. So the distiller keeps, right after the title and summary, a fixed
section of up to RULES_CAP one-line rules (a decision, a pitfall, a "do not"),
each with the date and a wikilink to the dated section it came from:

    ## Regras em vigor
    - 2026-11-04 · Sem 3DS no PayGate: o adquirente não suporta ([[shop-admin#PayGate sem 3DS (2026-11-04)]])

Newest first. A rule superseded by a newer one, or whose section of origin
was marked superseded, moves to `## Regras superadas` at the end of the note
with the date; nothing is deleted. Over the cap, the oldest lines move there
too, saying why. The narrative stays below, as before.

recall.py and vault_context.py read `lines_in_force()` to put these lines
first when the note is the one of the session's repo.
"""
import re
from datetime import datetime

from brain.core import LABELS, fold

RULES_CAP = 25
RULES_MAX_FROM_MODEL = 5
RULE_MAX_CHARS = 220
MARKER = "=== RULES ==="

IN_FORCE = tuple(l["rules_section"] for l in LABELS.values())
SUPERSEDED = tuple(l["rules_superseded"] for l in LABELS.values())
RULE_LINE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) · (.+?)(?: \(\[\[([^\]]+)\]\]\))?\s*$")
SUPERSEDES = re.compile(r"\s*\((?:supersedes|supera|substitui):\s*[\"“]?(.+?)[\"”]?\)\s*$", re.I)
MARKED = re.compile(r"⚠️ \*\*(Superseded|Superado)")


def heading_rx(names) -> re.Pattern:
    return re.compile(r"^## (" + "|".join(re.escape(n) for n in names) + r")[ \t]*$", re.M)


def section_span(txt: str, names):
    """(start of heading, end of body) of the first `## <name>` section, or
    None. The body ends at the next heading of any level."""
    m = heading_rx(names).search(txt)
    if not m:
        return None
    nxt = re.search(r"^#{1,6} ", txt[m.end():], re.M)
    return m.start(), (m.end() + nxt.start()) if nxt else len(txt)


def lines_in_force(txt: str) -> list:
    """The bullet lines of the "Rules in force" section, in order."""
    span = section_span(txt, IN_FORCE)
    if not span:
        return []
    return [l for l in txt[span[0]:span[1]].splitlines() if l.startswith("- ")]


def parse_model_output(out: str):
    """(block, rules) from what the distiller returned: the narrative block
    above the MARKER, the `- ` lines below it. A line may end with
    `(supersedes: <fragment>)` naming a rule it replaces. At most
    RULES_MAX_FROM_MODEL, each cut at RULE_MAX_CHARS."""
    if MARKER not in out:
        return out.strip(), []
    block, tail = out.split(MARKER, 1)
    rules = []
    for l in tail.splitlines():
        l = l.strip()
        if not l.startswith("- "):
            continue
        text = l[2:].strip()
        if re.match(r"^(none|nenhuma|empty|vazio)\.?$", text, re.I):
            continue
        sup = None
        m = SUPERSEDES.search(text)
        if m:
            sup = m.group(1).strip()
            text = text[:m.start()].strip()
        text = re.sub(r"^\d{4}-\d{2}-\d{2}\s*[·:-]\s*", "", text)      # the program dates it
        if len(text) > RULE_MAX_CHARS:
            text = text[:RULE_MAX_CHARS].rsplit(" ", 1)[0] + "…"
        if text:
            rules.append({"text": text, "supersedes": sup})
        if len(rules) >= RULES_MAX_FROM_MODEL:
            break
    return block.strip(), rules


def key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", fold(text))


def rule_line(day: str, text: str, link: str = "") -> str:
    return f"- {day} · {text}" + (f" ([[{link}]])" if link else "")


def ensure_section(txt: str, heading: str, at_top: bool) -> str:
    """The section present, empty. At the top: right after the title and
    the summary paragraph(s), before the first `## `. At the end: last."""
    if heading_rx([heading[3:]]).search(txt):
        return txt
    if at_top:
        m = re.search(r"^## ", txt, re.M)
        if m:
            return txt[:m.start()].rstrip("\n") + f"\n\n{heading}\n\n" + txt[m.start():]
    return txt.rstrip("\n") + f"\n\n{heading}\n"


def add_lines(txt: str, heading: str, new_lines: list, at_top: bool) -> str:
    """Insert lines at the top of a section (newest first), creating it."""
    txt = ensure_section(txt, heading, at_top)
    start, end = section_span(txt, [heading[3:]])
    body = txt[start:end]
    head_end = body.index("\n") + 1 if "\n" in body else len(body)
    rest = body[head_end:].lstrip("\n")
    if rest and not rest.startswith("- "):
        rest = "\n" + rest
    new_body = body[:head_end] + "\n" + "\n".join(new_lines) + "\n" + rest
    if not new_body.endswith("\n\n") and end < len(txt):
        new_body = new_body.rstrip("\n") + "\n\n"
    return txt[:start] + new_body + txt[end:]


def remove_lines(txt: str, heading: str, lines: list) -> str:
    start, end = section_span(txt, [heading[3:]])
    body = txt[start:end]
    gone = set(lines)
    kept = "\n".join(l for l in body.splitlines() if l not in gone)
    if end < len(txt):
        kept = kept.rstrip("\n") + "\n\n"
    return txt[:start] + kept + txt[end:]


def demote(txt: str, L: dict, lines: list, day: str, why: str = "") -> str:
    """Move rule lines from "in force" to "superseded", dated, never deleted."""
    if not lines:
        return txt
    in_force, sup = f"## {L['rules_section']}", f"## {L['rules_superseded']}"
    txt = remove_lines(txt, in_force, lines)
    stamp = f" — {L['rules_superseded_on']} {day}" + (f" ({why})" if why else "")
    return add_lines(txt, sup, [l + stamp for l in lines], at_top=False)


def apply(txt: str, rules: list, L: dict, day: str, note_name: str, section_title: str) -> tuple:
    """Write the model's rules into the note text: new lines on top of
    "Rules in force" with the date and `[[note#section]]`, the ones they
    supersede demoted, duplicates skipped, the cap enforced. Returns
    (new text, added, demoted)."""
    if not rules:
        return txt, 0, 0
    in_force = f"## {L['rules_section']}"
    existing = lines_in_force(txt)
    keys = {key(RULE_LINE.match(l).group(2)) if RULE_LINE.match(l) else key(l) for l in existing}
    to_demote, new_lines = [], []
    for r in rules:
        k = key(r["text"])
        if not k or k in keys:
            continue
        if r.get("supersedes"):
            frag = key(r["supersedes"])
            for l in existing:
                if frag and frag in key(l) and l not in to_demote:
                    to_demote.append(l)
        new_lines.append(rule_line(day, r["text"], f"{note_name}#{section_title}" if section_title else ""))
        keys.add(k)
    if not new_lines:
        return txt, 0, 0
    txt = demote(txt, L, to_demote, day)
    txt = add_lines(txt, in_force, new_lines, at_top=True)
    # the cap: the oldest fall off the bottom, into the history, saying why
    over = lines_in_force(txt)[RULES_CAP:]
    if over:
        txt = demote(txt, L, over, day, L["rules_cap_reason"].format(n=RULES_CAP))
    return txt, len(new_lines), len(to_demote) + len(over)


def demote_marked(txt: str, L: dict, day: str) -> tuple:
    """Rules whose section of origin now carries the superseded mark (the
    staleness check wrote it) go down too. Returns (text, how many)."""
    gone = []
    for l in lines_in_force(txt):
        m = RULE_LINE.match(l)
        if not m or not m.group(3) or "#" not in m.group(3):
            continue
        section = m.group(3).split("#", 1)[1]
        h = re.search(rf"^#{{2,3}} {re.escape(section)}\s*$", txt, re.M)
        if not h:
            continue
        nxt = re.search(r"^#{1,6} ", txt[h.end():], re.M)
        body = txt[h.end():h.end() + (nxt.start() if nxt else 400)][:400]
        if MARKED.search(body):
            gone.append(l)
    return demote(txt, L, gone, day), len(gone)


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")
