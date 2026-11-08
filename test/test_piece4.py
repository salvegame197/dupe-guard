"""
Tests for piece 4 of the 2026-11-08 plan (docs/IDEIAS-MYTHOS-2026-11-08.md,
items 3.4 and 3.7): a correction the user made becomes one draft line in
the repo's CLAUDE.md (own repo) or the native memory (someone else's repo),
with every guard around it; and the distiller keeps a "Rules in force"
section at the top of the project note that recall and the session
context read first.

Everything runs in a throwaway DUPE_GUARD_HOME with its own vault, repos
and transcripts folder. No model is called.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
sys.path.insert(0, str(ROOT))

from brain.core import project_folder_name          # noqa: E402


def line(**row):
    return json.dumps(row, ensure_ascii=False) + "\n"


def user(text, cwd, ts="2026-11-04T10:00:00Z"):
    return line(type="user", cwd=cwd, timestamp=ts, message={"role": "user", "content": text})


def model(text, cwd, ts="2026-11-04T10:00:20Z", edit=None):
    content = [{"type": "text", "text": text}]
    if edit:
        content.append({"type": "tool_use", "id": "t1", "name": "Edit",
                        "input": {"file_path": edit, "old_string": "a", "new_string": "b"}})
    return line(type="assistant", cwd=cwd, timestamp=ts, message={"role": "assistant", "content": content})


class Piece4Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dupe-guard-p4-")).resolve()
        self.home = self.tmp / "home"
        self.vault = self.tmp / "vault"
        self.repo = self.tmp / "billing-api"            # no remote: the user's own
        self.other = self.tmp / "client-app"            # someone else's remote
        self.transcripts = self.tmp / "transcripts"
        (self.vault / "Projects").mkdir(parents=True)
        for r in (self.repo, self.other):
            (r / "src").mkdir(parents=True)
            (r / "src" / "invoice.py").write_text("def total(): return 1\n")
            subprocess.run(["git", "init", "-q"], cwd=r, check=True)
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "."], cwd=r, check=True)
            subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init"],
                           cwd=r, check=True)
        subprocess.run(["git", "remote", "add", "origin", "https://github.com/acme/client-app.git"],
                       cwd=self.other, check=True)
        self.transcripts.mkdir()
        self.home.mkdir()
        (self.home / "config.json").write_text(json.dumps({"brain": {
            "enabled": True, "languages": ["pt"], "note_language": "pt-BR",
            "vault": str(self.vault), "transcripts": str(self.transcripts)}}))
        self.env = {**os.environ, "DUPE_GUARD_HOME": str(self.home), "DUPE_GUARD_VAULT": str(self.vault),
                    "DUPE_GUARD_TRANSCRIPTS": str(self.transcripts), "DUPE_GUARD_BRAIN": "1"}
        self.env.pop("DUPE_GUARD_DISABLE", None)
        for k, v in self.env.items():
            if k.startswith("DUPE_GUARD"):
                os.environ[k] = v
        # modules cache HOME_DIR at import: import fresh for every test
        for m in [m for m in sys.modules if m.startswith("brain")]:
            del sys.modules[m]
        from brain import learned
        from brain.core import load_config
        self.learned = learned
        self.cfg = load_config()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def cfg_with(self, **learned):
        cfg = dict(self.cfg)
        cfg["learned"] = {"enabled": True, "own_remotes": [], "max_per_session": 3, **learned}
        return cfg

    def commit(self, repo, msg="ok"):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg],
                       cwd=repo, check=True)

    def transcript(self, name, rows):
        p = self.transcripts / project_folder_name(str(self.repo)) / f"{name}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(rows))
        return p

    def triad_rows(self, cwd, prompt="nao vamos fazer 3ds na paygate! o adquirente nao suporta",
                   reply="Combinado: sem 3DS no PayGate. Tiro o fluxo.", edit=None, ts="2026-11-04T10:00:00Z"):
        edit = edit or str(Path(cwd) / "src" / "invoice.py")
        return [user("vamos revisar o checkout do paygate hoje, pode ser?", cwd, "2026-11-04T09:59:00Z"),
                model("Claro, começo pelo fluxo de 3DS.", cwd, "2026-11-04T09:59:20Z"),
                user(prompt, cwd, ts), model(reply, cwd, edit=edit)]


class Detection(Piece4Case):
    """3.4: what counts as the triad."""

    def test_correction_vs_question_vs_bug_report(self):
        P = self.learned.patterns(self.cfg_with())
        yes = ["nao vamos fazer 3ds na paygate!", "o codigo é char(11), eu já te disse",
               "nunca um cliente vai avisar que vai cair", "nao, o tenant vem do request, nao do user",
               "tem que ser em ingles no log"]
        no = ["nao entendi esse confirmar reembolso", "nao sei oque é isso ai",
              "tu consegue ver os logs? ver oque está dando errado?", "no login deu errado: invalid_request",
              "nao precisa do aviso, é que nao entendi o fluxo", "x" * 700]
        for t in yes:
            self.assertTrue(self.learned.is_correction(t, P), t)
        for t in no:
            self.assertFalse(self.learned.is_correction(t, P), t)
        # a question mark does not disqualify a strong rule
        self.assertTrue(self.learned.is_correction("ingressos max_uses tem que ser 999 entende?", P))

    def test_admission_anywhere_or_acceptance_at_the_start(self):
        P = self.learned.patterns(self.cfg_with())
        self.assertEqual(self.learned.admitted("Conferi de novo e você tem razão, o campo é char.", P), "você tem razão")
        self.assertEqual(self.learned.admitted("Combinado: sem 3DS.", P), "Combinado")
        self.assertIsNone(self.learned.admitted("Movimentações está pronta. Agora o filtro.", P))
        self.assertIsNone(self.learned.admitted("O fluxo está certo como está; combinado então segue.", P))

    def test_triad_needs_all_three_and_a_real_edit(self):
        cwd = str(self.repo)
        cfg = self.cfg_with()
        full = self.transcript("full", self.triad_rows(cwd))
        self.assertEqual(len(self.learned.triads(full, cfg)), 1)
        t = self.learned.triads(full, cfg)[0]
        self.assertEqual((t["prompt_idx"], t["admission"]), (3, "Combinado"))   # events()[0] is the cwd
        self.assertFalse(t["self_recorded"])
        no_admit = self.transcript("na", self.triad_rows(cwd, reply="Movimentações está pronta. Agora o filtro."))
        self.assertEqual(self.learned.triads(no_admit, cfg), [])
        no_edit = self.transcript("ne", self.triad_rows(cwd)[:3] + [model("Combinado: sem 3DS.", cwd)])
        self.assertEqual(self.learned.triads(no_edit, cfg), [])
        scratch = self.transcript("sc", self.triad_rows(cwd, edit="/tmp/claude-1/scratch/plan.md"))
        self.assertEqual(self.learned.triads(scratch, cfg), [])
        mem = str(self.transcripts / project_folder_name(cwd) / "memory" / "paygate-sem-3ds.md")
        selfrec = self.transcript("sr", self.triad_rows(cwd, edit=mem))
        self.assertTrue(self.learned.triads(selfrec, cfg)[0]["self_recorded"])
        # not a person: a task notification that happens to say "errado"
        notif = self.transcript("nt", self.triad_rows(cwd, prompt="<task-notification>deu tudo errado, nao vamos usar</task-notification>"))
        self.assertEqual(self.learned.triads(notif, cfg), [])
        # the brain's own child
        from brain.distill import INSTRUCTIONS
        worker = self.transcript("wk", [user(INSTRUCTIONS[:200], cwd)]
                                 + self.triad_rows(cwd)[2:])
        self.assertEqual(self.learned.triads(worker, cfg), [])

    def test_line_keeps_the_users_words_with_date_session_and_polarity(self):
        cfg = self.cfg_with()
        P = self.learned.patterns(cfg)
        t = {"prompt": "cara mas eu já te pedi para nunca usar tabs... porque o lint reclama"}
        out = self.learned.line_for(t, "2026-11-07", "ff00678e-1234", cfg, P)
        self.assertEqual(out, '- 2026-11-07 · sessão ff00678e · não fazer: "Eu já te pedi para nunca usar tabs. porque o lint reclama"')
        pos = self.learned.line_for({"prompt": "vi um problema só.. ta tudo em portugues lá no log tem que ser em ingles..."},
                                    "2026-11-04", "3c4aa1a3", cfg, P)
        self.assertIn("· fazer: \"Ta tudo em portugues lá no log tem que ser em ingles\"", pos)
        # a secret in the correction never reaches the line
        sec = self.learned.line_for({"prompt": "nao vamos usar essa chave sk-ant-abcdefghijklmnopqrstuvwxyz123456 nunca mais"},
                                    "2026-11-04", "s", cfg, P)
        self.assertNotIn("sk-ant-abc", sec)
        self.assertIn("[REDACTED]", sec)
        self.assertTrue(self.learned.MARK.match(out))


class Destination(Piece4Case):
    """3.4: own repo -> CLAUDE.md; someone else's or no repo -> native memory."""

    def test_no_remote_is_own_and_a_configured_prefix_is_own(self):
        cfg = self.cfg_with()
        kind, path, repo = self.learned.destination(str(self.repo), cfg)
        self.assertEqual((kind, path, repo), ("claude_md", self.repo / "CLAUDE.md", "billing-api"))
        kind, path, repo = self.learned.destination(str(self.other), cfg)
        self.assertEqual(kind, "memory")
        self.assertEqual(path, self.transcripts / project_folder_name(str(self.other)) / "memory" / "aprendido-client-app.md")
        cfg = self.cfg_with(own_remotes=["github.com/acme/"])
        self.assertEqual(self.learned.destination(str(self.other), cfg)[0], "claude_md")
        self.assertTrue(self.learned.owned("", ["github.com/acme/"], remote="git@github.com:acme/x.git"))
        self.assertTrue(self.learned.owned("", ["github.com/acme/"], remote="ssh://git@github.com/acme/x"))
        self.assertFalse(self.learned.owned("", ["github.com/acme/"], remote="https://github.com/acme-corp/x.git"))
        self.assertEqual(self.learned.destination("", cfg)[0], None)
        # a subfolder of the repo still resolves to the repo root
        self.assertEqual(self.learned.destination(str(self.repo / "src"), self.cfg_with())[1], self.repo / "CLAUDE.md")

    def test_outside_any_repo_goes_to_the_memory_of_that_folder(self):
        scratch = self.tmp / "notes"
        scratch.mkdir()
        kind, path, repo = self.learned.destination(str(scratch), self.cfg_with())
        self.assertEqual(kind, "memory")
        self.assertEqual(path.name, "aprendido-notes.md")
        self.assertEqual(path.parent.parent.name, project_folder_name(str(scratch)))


class Writing(Piece4Case):
    """3.4: the section, the order, the duplicate check, the cap, the log."""

    def run_one(self, name, rows, cwd=None, cfg=None, sid=None):
        tp = self.transcript(name, rows)
        return self.learned.run(str(tp), sid or name + "-sid-0000", cwd or str(self.repo), cfg or self.cfg_with(),
                                datetime(2026, 11, 4, 10, 5))

    def test_claude_md_gets_the_section_at_the_end_newest_first_without_duplicates(self):
        md = self.repo / "CLAUDE.md"
        md.write_text("# billing-api\n\nRun tests with `make test`.\n")
        self.commit(self.repo)
        r = self.run_one("a", self.triad_rows(str(self.repo)))
        self.assertEqual((r["triads"], r["written"]), (1, 1))
        text = md.read_text()
        self.assertTrue(text.startswith("# billing-api\n\nRun tests with `make test`.\n\n## Aprendido (revisar)\n\n- 2026-11-04 · sessão a-sid-00 · não fazer: \"Nao vamos fazer 3ds na paygate"), text)
        # second session, a different rule: on top of the section; the same
        # rule again (other words around it, same text): not written
        self.commit(self.repo)
        r = self.run_one("b", self.triad_rows(str(self.repo), prompt="o codigo é char(11), eu já te disse, nao int",
                                                reply="Tem razão, corrijo o tipo.", ts="2026-11-05T10:00:00Z"))
        self.assertEqual(r["written"], 1)
        self.commit(self.repo)
        r = self.run_one("c", self.triad_rows(str(self.repo), prompt="NAO VAMOS FAZER 3DS NA PAYGATE!! o adquirente nao suporta."))
        self.assertEqual((r["written"], r["duplicate"]), (0, 1))
        lines = [l for l in md.read_text().splitlines() if l.startswith("- ")]
        self.assertEqual(len(lines), 2)
        self.assertIn("codigo é char(11)", lines[0])          # newest first
        self.assertIn("3ds na paygate", lines[1])
        log = [json.loads(l) for l in (self.home / "brain" / "aprendido-log.jsonl").read_text().splitlines()]
        self.assertEqual([e["status"] for e in log], ["written", "written", "duplicate"])
        self.assertEqual(log[0]["triad"]["prompt"], 3)
        self.assertEqual(log[0]["kind"], "claude_md")

    def test_at_most_three_lines_per_session(self):
        cwd = str(self.repo)
        rows = []
        for i, p in enumerate(["nunca rodar a suite e2e aqui", "sempre usar o tenant do request",
                               "nao vamos usar redis para sessao", "tem que ser utf-8 no export"]):
            rows += [user(p, cwd, f"2026-11-04T10:0{i}:00Z"),
                     model("Tem razão, corrijo.", cwd, f"2026-11-04T10:0{i}:20Z", edit=f"{cwd}/src/invoice.py")]
        r = self.run_one("cap", rows)
        self.assertEqual((r["triads"], r["written"], r["skipped"]), (4, 3, 1))
        self.assertEqual(sum(1 for l in (self.repo / "CLAUDE.md").read_text().splitlines() if l.startswith("- ")), 3)

    def test_a_foreign_uncommitted_change_makes_the_line_wait_and_a_commit_releases_it(self):
        md = self.repo / "CLAUDE.md"
        md.write_text("# billing-api\n")
        self.commit(self.repo)
        md.write_text("# billing-api\n\nWork in progress by a human.\n")        # not committed
        r = self.run_one("w", self.triad_rows(str(self.repo)))
        self.assertEqual((r["written"], r["pending"]), (0, 1))
        self.assertNotIn("Aprendido", md.read_text())                          # untouched
        pend = (self.home / "brain" / "aprendido-pendente.jsonl").read_text().splitlines()
        self.assertEqual(len(pend), 1)
        # the human commits; the next session end (any session) places it
        self.commit(self.repo)
        r = self.run_one("x", [user("oi, tudo bem por ai? vamos olhar o relatorio de hoje", str(self.repo)),
                               model("Vamos.", str(self.repo))])
        self.assertEqual(r["triads"], 0)
        self.assertIn('não fazer: "Nao vamos fazer 3ds na paygate', md.read_text())
        self.assertEqual((self.home / "brain" / "aprendido-pendente.jsonl").read_text(), "")
        log = [json.loads(l) for l in (self.home / "brain" / "aprendido-log.jsonl").read_text().splitlines()]
        self.assertEqual([e["status"] for e in log], ["pending", "written"])
        self.assertTrue(log[1].get("retried"))

    def test_our_own_uncommitted_line_does_not_block_the_next_one(self):
        md = self.repo / "CLAUDE.md"
        md.write_text("# billing-api\n")
        self.commit(self.repo)
        self.run_one("a", self.triad_rows(str(self.repo)))
        r = self.run_one("b", self.triad_rows(str(self.repo), prompt="nunca rodar a suite e2e aqui, ela derruba o banco",
                                                reply="Entendido, tiro do script."))
        self.assertEqual(r["written"], 1)
        self.assertEqual(sum(1 for l in md.read_text().splitlines() if l.startswith("- ")), 2)

    def test_untracked_claude_md_written_by_a_human_waits(self):
        (self.repo / "CLAUDE.md").write_text("# billing-api\n\nhand-written, never committed\n")
        r = self.run_one("u", self.triad_rows(str(self.repo)))
        self.assertEqual(r["pending"], 1)

    def test_someone_elses_repo_goes_to_native_memory_with_frontmatter_and_pointer(self):
        cwd = str(self.other)
        r = self.run_one("m", self.triad_rows(cwd), cwd=cwd)
        self.assertEqual(r["written"], 1)
        mem = self.transcripts / project_folder_name(cwd) / "memory"
        note = (mem / "aprendido-client-app.md").read_text()
        self.assertTrue(note.startswith("---\nname: aprendido-client-app\ndescription: Regras rascunhadas"))
        self.assertIn("  type: feedback\n", note)
        self.assertIn("# Aprendido (revisar) — client-app\n\n- 2026-11-04 · sessão m-sid-00 · não fazer:", note)
        index = (mem / "MEMORY.md").read_text()
        self.assertIn("](aprendido-client-app.md)", index)
        self.assertFalse((self.other / "CLAUDE.md").exists())
        # second line on top, pointer not repeated
        self.run_one("n", self.triad_rows(cwd, prompt="sempre usar o tenant do request, nunca do user",
                                          reply="Tem razão, corrijo."), cwd=cwd)
        note = (mem / "aprendido-client-app.md").read_text()
        bullets = [l for l in note.splitlines() if l.startswith("- ")]
        self.assertEqual(len(bullets), 2)
        self.assertIn("producer do request", bullets[0])
        self.assertEqual((mem / "MEMORY.md").read_text().count("aprendido-client-app.md"), 1)

    def test_self_recorded_and_disabled_write_nothing(self):
        mem = str(self.transcripts / project_folder_name(str(self.repo)) / "memory" / "paygate.md")
        r = self.run_one("s", self.triad_rows(str(self.repo), edit=mem))
        self.assertEqual((r["triads"], r["written"], r["skipped"]), (1, 0, 1))
        self.assertFalse((self.repo / "CLAUDE.md").exists())
        r = self.run_one("d", self.triad_rows(str(self.repo)), cfg=self.cfg_with(enabled=False))
        self.assertEqual(r, {"triads": 0, "written": 0, "pending": 0, "duplicate": 0, "skipped": 0, "lines": []})
        self.assertFalse((self.repo / "CLAUDE.md").exists())

    def test_session_end_hook_runs_the_extractor_and_says_so_in_the_raw_block(self):
        (self.vault / "Projects" / "billing-api.md").write_text("# billing-api\n\nnote\n")
        tp = self.transcript("hook", self.triad_rows(str(self.repo)) + [
            user("agora roda os testes e me diz o resultado por favor", str(self.repo), "2026-11-04T10:02:00Z"),
            model("Rodei: 12 passando.", str(self.repo), "2026-11-04T10:02:20Z")])
        r = subprocess.run([sys.executable, str(BRAIN / "session_log.py")],
                           input=json.dumps({"transcript_path": str(tp), "session_id": "hook-sid-1", "cwd": str(self.repo),
                                             "reason": "exit"}),
                           capture_output=True, text=True, timeout=60, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("## Aprendido (revisar)", (self.repo / "CLAUDE.md").read_text())
        raw = next((self.vault / "Sessions" / "Raw").glob("*.md")).read_text()
        self.assertIn("**Aprendido por correção:** 1 → `CLAUDE.md`", raw)

    def test_hit_rate_counts_survivors_and_deletions(self):
        md = self.repo / "CLAUDE.md"
        md.write_text("# billing-api\n")
        self.commit(self.repo)
        self.run_one("a", self.triad_rows(str(self.repo)))
        self.commit(self.repo)                       # approved
        self.run_one("b", self.triad_rows(str(self.repo), prompt="nunca rodar a suite e2e aqui, derruba o banco",
                                          reply="Entendido, tiro."))
        md.write_text("\n".join(l for l in md.read_text().splitlines() if "e2e" not in l) + "\n")   # rejected
        from brain import hit_rate
        status, rows = hit_rate.learned_report(self.home / "brain" / "aprendido-log.jsonl")
        self.assertEqual(status["written"], 2)
        self.assertEqual([(r["present"], r["committed"]) for r in rows], [(True, True), (False, False)])
        out = subprocess.run([sys.executable, str(BRAIN / "hit_rate.py")], capture_output=True, text=True,
                             env=self.env, timeout=60).stdout
        self.assertIn("LEARNED FROM CORRECTIONS (2 triads logged", out)
        self.assertIn("still in the file           1    50.0% of 2 written", out)
        self.assertIn("CLAUDE.md lines             2   1 in HEAD", out)


if __name__ == "__main__":
    unittest.main()


SYNTHETIC_NOTE = """---
tags: [project]
---

# billing-api

Invoices and payments for the shop. Node + Postgres.

## Arquitetura

Invoices are generated by a nightly job and stored in Postgres.

## Decisões

- Retries use exponential backoff.

## Relacionados

- [[Payments]]
"""

PT_LABELS = {"rules_section": "Regras em vigor", "rules_superseded": "Regras superadas",
             "rules_superseded_on": "superada em", "rules_cap_reason": "saiu do teto de {n}"}


class RulesSection(Piece4Case):
    """3.7: the fixed section at the top, the cap, the demotion."""

    def setUp(self):
        super().setUp()
        from brain import rules
        self.rules = rules

    def test_parse_model_output_splits_block_and_rules(self):
        out = ("## Sem 3DS no PayGate 🔑\n\nO adquirente não suporta; o débito segue fora.\n\n"
               "=== RULES ===\n- Não fazer 3DS no PayGate: o adquirente não suporta.\n"
               "- 2026-11-04 · O débito fica fora do PayGate (supersedes: débito pelo PayGate)\n"
               "- none\n- a\n- b\n- c\n- d\n")
        block, found = self.rules.parse_model_output(out)
        self.assertEqual(block, "## Sem 3DS no PayGate 🔑\n\nO adquirente não suporta; o débito segue fora.")
        self.assertEqual(len(found), 5)                                  # cap from the model: 5
        self.assertEqual(found[0], {"text": "Não fazer 3DS no PayGate: o adquirente não suporta.", "supersedes": None})
        self.assertEqual(found[1], {"text": "O débito fica fora do PayGate", "supersedes": "débito pelo PayGate"})
        self.assertEqual(self.rules.parse_model_output("## Só bloco\n\ntexto")[1], [])
        self.assertEqual(self.rules.parse_model_output("## Bloco\n\n=== RULES ===\n- none\n")[1], [])

    def test_rules_go_right_after_the_summary_newest_first_dated_and_linked(self):
        txt, added, demoted = self.rules.apply(
            SYNTHETIC_NOTE, [{"text": "Sem 3DS no PayGate: o adquirente não suporta", "supersedes": None}],
            PT_LABELS, "2026-11-04", "billing-api", "Sem 3DS no PayGate 🔑 (2026-11-04)")
        self.assertEqual((added, demoted), (1, 0))
        self.assertIn("Node + Postgres.\n\n## Regras em vigor\n\n- 2026-11-04 · Sem 3DS no PayGate: o adquirente não suporta "
                      "([[billing-api#Sem 3DS no PayGate 🔑 (2026-11-04)]])\n\n## Arquitetura\n", txt)
        txt2, added, _ = self.rules.apply(txt, [{"text": "Nunca mover invoices para fila", "supersedes": None},
                                                {"text": "sem 3ds no paygate: o adquirente nao suporta", "supersedes": None}],
                                          PT_LABELS, "2026-11-05", "billing-api", "Fila (2026-11-05)")
        self.assertEqual(added, 1)                                       # the duplicate (folded) is skipped
        lines = self.rules.lines_in_force(txt2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("- 2026-11-05 · Nunca mover"))
        self.assertTrue(lines[1].startswith("- 2026-11-04 · Sem 3DS"))
        self.assertEqual(txt2.count("## Regras em vigor"), 1)

    def test_a_superseding_rule_moves_the_old_one_to_the_history_with_the_date(self):
        txt, _, _ = self.rules.apply(SYNTHETIC_NOTE, [{"text": "Débito passa pelo PayGate", "supersedes": None}],
                                     PT_LABELS, "2026-10-01", "billing-api", "Débito (2026-10-01)")
        txt, added, demoted = self.rules.apply(
            txt, [{"text": "Débito fica fora do PayGate: exige 3DS", "supersedes": "passa pelo PayGate"}],
            PT_LABELS, "2026-11-04", "billing-api", "Sem 3DS (2026-11-04)")
        self.assertEqual((added, demoted), (1, 1))
        self.assertEqual(len(self.rules.lines_in_force(txt)), 1)
        self.assertIn("Débito fica fora", self.rules.lines_in_force(txt)[0])
        self.assertTrue(txt.rstrip().endswith(
            "## Regras superadas\n\n- 2026-10-01 · Débito passa pelo PayGate ([[billing-api#Débito (2026-10-01)]]) — superada em 2026-11-04"), txt)
        self.assertLess(txt.index("## Relacionados"), txt.index("## Regras superadas"))   # history at the very end
        # a note with no closing section: the next block still lands ABOVE the history
        from brain import distill
        bare = txt.replace("## Relacionados\n\n- [[Payments]]\n", "")
        note = self.vault / "Projects" / "billing-api.md"
        note.write_text(bare)
        distill.insert(note, "## Novo bloco\n\nTexto.", "sid-9", self.cfg, "2026-11-06")
        new = note.read_text()
        self.assertLess(new.index("## Novo bloco"), new.index("## Regras superadas"))

    def test_cap_of_25_pushes_the_oldest_down_saying_why(self):
        txt = SYNTHETIC_NOTE
        for i in range(27):
            txt, _, _ = self.rules.apply(txt, [{"text": f"Regra número {i}", "supersedes": None}],
                                         PT_LABELS, f"2026-10-{(i % 28) + 1:02d}", "billing-api", f"S{i}")
        lines = self.rules.lines_in_force(txt)
        self.assertEqual(len(lines), 25)
        self.assertIn("Regra número 26", lines[0])
        self.assertIn("Regra número 2", lines[-1])
        self.assertIn("- 2026-10-01 · Regra número 0 ([[billing-api#S0]]) — superada em 2026-10-26 (saiu do teto de 25)", txt)
        self.assertIn("Regra número 1 ([[billing-api#S1]]) — superada em", txt)

    def test_a_rule_whose_section_was_marked_superseded_goes_down(self):
        txt = SYNTHETIC_NOTE.replace("## Decisões\n", "## Decisões\n\n> ⚠️ **Superseded by Fila** — moved to a queue _(marked automatically, 3/3)_\n")
        txt = txt.replace("Node + Postgres.\n", "Node + Postgres.\n\n## Regras em vigor\n\n"
                          "- 2026-10-01 · Retries com backoff ([[billing-api#Decisões]])\n"
                          "- 2026-10-02 · Job noturno fica ([[billing-api#Arquitetura]])\n")
        new, n = self.rules.demote_marked(txt, PT_LABELS, "2026-11-04")
        self.assertEqual(n, 1)
        self.assertEqual([l[:31] for l in self.rules.lines_in_force(new)], ["- 2026-10-02 · Job noturno fica"])
        self.assertIn("- 2026-10-01 · Retries com backoff ([[billing-api#Decisões]]) — superada em 2026-11-04", new)

    def test_distill_insert_writes_block_and_rules_and_staleness_skips_the_rule_sections(self):
        from brain import distill
        note = self.vault / "Projects" / "billing-api.md"
        note.write_text(SYNTHETIC_NOTE)
        out = ("## Job noturno fica 🔑\n\nUma fila dobraria os modos de falha.\n\n"
               "=== RULES ===\n- Não mover invoices para fila: o job noturno fica.\n")
        self.assertTrue(distill.insert(note, out, "sid-1", self.cfg, "2026-11-04"))
        txt = note.read_text()
        self.assertIn("## Regras em vigor\n\n- 2026-11-04 · Não mover invoices para fila: o job noturno fica. "
                      "([[billing-api#Job noturno fica 🔑]])\n\n## Arquitetura", txt)
        self.assertIn("## Job noturno fica 🔑\n\nUma fila dobraria os modos de falha.\n\n<!-- distilled:sid-1 -->\n\n## Relacionados", txt)
        self.assertNotIn("=== RULES ===", txt)
        self.assertFalse(distill.insert(note, out, "sid-1", self.cfg, "2026-11-04"))     # idempotent
        titles = [t for _, t, _ in distill.sections(txt, "sid-1")]
        self.assertNotIn("Regras em vigor", titles)
        self.assertIn("Arquitetura", titles)
        # the staleness mark on a section demotes the rules that cite it
        distill.mark_superseded(note, [{"section": "## Decisões", "reason": "no backoff", "votes": 3}], "Job noturno", 3)
        note.write_text(note.read_text().replace("- 2026-11-04 · Não mover invoices para fila: o job noturno fica. ([[billing-api#Job noturno fica 🔑]])",
                                                 "- 2026-11-04 · Não mover invoices para fila: o job noturno fica. ([[billing-api#Decisões]])"))
        self.assertEqual(distill.demote_rules(note, self.cfg, "2026-11-05"), 1)
        self.assertIn("## Regras superadas\n\n- 2026-11-04 · Não mover", note.read_text())

    def test_distill_dry_run_preview_shows_the_rules_without_a_model(self):
        from brain import distill
        note = self.vault / "Projects" / "billing-api.md"
        note.write_text(SYNTHETIC_NOTE)
        diff = distill.preview(note, "## Fila 🔑\n\nTexto.\n\n=== RULES ===\n- Sem fila.\n", "sid-2", self.cfg, "2026-11-04")
        self.assertIn("+## Regras em vigor", diff)
        self.assertIn("+- 2026-11-04 · Sem fila. ([[billing-api#Fila 🔑]])", diff)
        self.assertEqual(note.read_text(), SYNTHETIC_NOTE)

    def test_prompt_carries_the_rules_instruction_in_the_notes_language(self):
        from brain import distill
        note = self.vault / "Projects" / "billing-api.md"
        note.write_text(SYNTHETIC_NOTE)
        p = distill.prompt_for({"day": "2026-11-04", "session_id": "x", "raw": "/nope", "transcript": "/nope"},
                               note, self.cfg, "[user] oi", SYNTHETIC_NOTE)
        self.assertIn("=== RULES ===", p)
        self.assertIn('("Regras em vigor")', p)
        self.assertIn("ONE sentence in pt-BR", p)


class RulesRead(Piece4Case):
    """3.7: recall and the session context read the rules first."""

    def run_py(self, script, payload=None, args=()):
        r = subprocess.run([sys.executable, str(BRAIN / script), *args],
                           input=json.dumps(payload) if payload is not None else "",
                           capture_output=True, text=True, timeout=60, env=self.env)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r.stdout

    def context(self, out):
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else None

    def with_rules(self):
        note = self.vault / "Projects" / "billing-api.md"
        note.write_text(SYNTHETIC_NOTE.replace(
            "Node + Postgres.\n", "Node + Postgres.\n\n## Regras em vigor\n\n"
            "- 2026-11-04 · Não mover invoices para fila: o job noturno fica ([[billing-api#Job noturno (2026-11-04)]])\n"
            "- 2026-11-03 · Retries com backoff exponencial, nunca linear ([[billing-api#Decisões]])\n"))
        self.run_py("ksearch.py", args=["--reindex"])
        return note

    def test_recall_snippet_comes_from_the_rules_for_the_sessions_own_note(self):
        self.with_rules()
        out = self.context(self.run_py("recall.py", {"prompt": "vamos revisar o nightly job das invoices do billing-api e o backoff dos retries",
                                                     "session_id": "r1", "cwd": str(self.repo)}))
        self.assertIsNotNone(out)
        page = out[out.index("**billing-api**"):].splitlines()
        self.assertTrue(page[1].startswith("  - - 2026-11-04 · Não mover invoices para fila") or
                        "Não mover invoices para fila" in page[1], page[1])
        self.assertNotIn("[[billing-api#", page[1])
        # from another folder the same note shows its body snippet, unless a
        # term matches inside the rules
        out = self.context(self.run_py("recall.py", {"prompt": "vamos revisar o schema do postgres do billing-api hoje",
                                                     "session_id": "r2", "cwd": str(self.other)}))
        page = out[out.index("**billing-api**"):].splitlines()
        self.assertIn("Postgres", page[1])
        self.assertNotIn("Regras", page[1])

    def test_session_context_injects_the_rules_of_the_cwd_note(self):
        self.with_rules()
        out = self.context(self.run_py("vault_context.py", {"cwd": str(self.repo), "session_id": "c1"}))
        self.assertIn("## Regras em vigor de billing-api", out)
        self.assertIn("- 2026-11-04 · Não mover invoices para fila: o job noturno fica ([[billing-api#Job noturno (2026-11-04)]])", out)
        out = self.context(self.run_py("vault_context.py", {"cwd": str(self.other), "session_id": "c2"}))
        self.assertNotIn("Regras em vigor", out or "")
