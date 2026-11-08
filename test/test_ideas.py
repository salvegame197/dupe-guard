"""
Tests for piece 2 of the 2026-11-08 plan (docs/IDEIAS-MYTHOS-2026-11-08.md,
items 3.1 and 3.5): recall answers only a person, mutes the chat exports in
a code session, injects no catalog card; the raw session log, the proposals
page and the stored questions stay out of the production index; the
session-start context drops the "when to use" block and keeps the most
recent pages when a folder is over its limit.

Everything runs in a throwaway DUPE_GUARD_HOME with its own vault and repo.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
sys.path.insert(0, str(ROOT))

NOTE = """# billing-api

## Architecture

Invoices are generated nightly by the invoice worker; retries use exponential backoff.
"""
TRIP = """---
tags: [pessoal, viagem]
---
# Viagem para Trancoso

Planejamento da viagem: o invoice worker do hotel cobra a reserva com backoff e
o lugar fica perto da praia; levar o carro ou alugar?
"""


class IdeasCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dupe-guard-ideas-"))
        self.home = self.tmp / "home"
        self.vault = self.tmp / "vault"
        self.repo = self.tmp / "billing-api"
        self.scratch = self.tmp / "scratch"
        (self.vault / "Projects").mkdir(parents=True)
        (self.vault / "Projects" / "billing-api.md").write_text(NOTE)
        (self.vault / "Claude Web" / "Viagens").mkdir(parents=True)
        (self.vault / "Claude Web" / "Viagens" / "Viagem para Trancoso.md").write_text(TRIP)
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "invoice.py").write_text("def total(): return 1\n")
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.scratch.mkdir()
        (self.tmp / "transcripts").mkdir()
        self.env = {**os.environ, "DUPE_GUARD_HOME": str(self.home), "DUPE_GUARD_VAULT": str(self.vault),
                    "DUPE_GUARD_TRANSCRIPTS": str(self.tmp / "transcripts"), "DUPE_GUARD_BRAIN": "1"}
        self.env.pop("DUPE_GUARD_DISABLE", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_py(self, script, payload=None, args=(), env=None, cwd=None):
        r = subprocess.run([sys.executable, str(BRAIN / script if not script.startswith("/") else script), *args],
                           input=json.dumps(payload) if payload is not None else "",
                           capture_output=True, text=True, timeout=60, env=env or self.env, cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r.stdout

    def context(self, out):
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else None

    def reindex(self, *args):
        self.run_py("ksearch.py", args=["--reindex", *args])

    def log_rows(self):
        p = self.home / "brain" / "recall-log.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def recall(self, prompt, sid, cwd):
        return self.context(self.run_py("recall.py", {"prompt": prompt, "session_id": sid, "cwd": cwd}))


class NonHumanPrompts(IdeasCase):
    """3.1: what the harness sends through UserPromptSubmit is skipped before
    any search, and the log says so, so hit_rate can count it."""

    PROMPT = "how should exponential backoff work for the billing-api invoice worker retries"

    def test_prefixes_and_short_prompts_are_skipped_and_logged(self):
        self.reindex()
        cases = {
            "task-notification": "<task-notification>\n<task-id>b0q</task-id>\n" + self.PROMPT,
            "system": "<system-reminder>" + self.PROMPT,
            "local-command": "<local-command-stdout>" + self.PROMPT,
            "image": "[Image #1] " + self.PROMPT,
            "continuation": "This session is being continued from a previous conversation. " + self.PROMPT,
            "command": "<command-name>/commit</command-name>" + self.PROMPT,
            "skill": "<command-message>x</command-message>" + self.PROMPT,
            "skill ": "---\nname: vault-sync\ndescription: " + self.PROMPT,
            "cross-session": "<cross-session-context>" + self.PROMPT,
            "short": "ok, go ahead with it",
            "short ": "https://example.com/a/b/c/d/e/f/g " + "`code only`",
        }
        for reason, prompt in cases.items():
            self.assertIsNone(self.recall(prompt, "nh1", str(self.repo)), reason)
        rows = self.log_rows()
        self.assertEqual(len(rows), len(cases))
        self.assertTrue(all(r["decision"] == "skip:non-human" for r in rows), rows)
        self.assertEqual([r["reason"] for r in rows], [k.strip() for k in cases])
        self.assertTrue(all(r["terms"] == [] and r["picked"] == [] for r in rows))
        # the same words typed by a person still work
        self.assertIn("billing-api", self.recall(self.PROMPT, "nh2", str(self.repo)))
        self.assertEqual(self.log_rows()[-1]["decision"], "inject")

    def test_non_human_is_decided_on_the_text_alone(self):
        from brain.recall import non_human
        self.assertEqual(non_human("  <task-notification>…"), "task-notification")
        self.assertEqual(non_human(""), "empty")
        self.assertEqual(non_human("sim, pode"), "short")
        self.assertEqual(non_human("ok: https://x.y/" + "z" * 40 + " /Users/me/some/file.pcap"), "short")
        self.assertEqual(non_human("como era aquela regra da catraca no gate_control?"), "")
        # a person pasting a snippet is a person: words outside the code count
        self.assertEqual(non_human("por que isso quebra?\n```\nraise\n```\nno worker de invoice"), "")


class CodeSession(IdeasCase):
    """3.1: in a code session the chat exports have weight 0; outside, the
    behaviour is as before."""

    PROMPT = "billing-api invoice worker backoff cobra a reserva da viagem para trancoso"

    def test_chat_exports_are_muted_only_in_a_code_session(self):
        self.reindex()
        chat = self.recall(self.PROMPT, "cs-chat", str(self.scratch))
        self.assertIsNotNone(chat)
        self.assertIn("Viagem para Trancoso", chat)
        code = self.recall(self.PROMPT, "cs-code", str(self.repo / "src"))
        self.assertIsNotNone(code)
        self.assertNotIn("Viagem para Trancoso", code)
        self.assertIn("billing-api", code)
        modes = {r["session"]: r.get("mode") for r in self.log_rows()}
        self.assertEqual(modes, {"cs-chat": "chat", "cs-code": "code"})
        ids = [r["ids"] for r in self.log_rows() if r["session"] == "cs-code"][0]
        self.assertTrue(all(not i.startswith("Claude Web/") for i in ids), ids)

    def test_a_repo_edited_this_session_makes_it_a_code_session(self):
        self.reindex()
        # the guard saw a Write inside the repo: it records the repo root
        self.run_py(str(ROOT / "guard.py"), {"tool_name": "Write", "session_id": "cs-guard", "cwd": str(self.scratch),
                                             "tool_input": {"file_path": str(self.repo / "src" / "new.py"),
                                                            "content": "def total_again(): return 2\n"}})
        st = json.loads((self.home / "state" / "cs-guard.json").read_text())
        self.assertEqual(st["repos"], [str(self.repo.resolve())])
        # the prompt comes from ~ (no repo), yet the session is about code
        out = self.recall(self.PROMPT, "cs-guard", str(self.scratch))
        self.assertIsNotNone(out)
        self.assertNotIn("Viagem para Trancoso", out)

    def test_in_git_repo_walks_up_and_ignores_a_dotfiles_home(self):
        from brain.recall import in_git_repo
        self.assertTrue(in_git_repo(str(self.repo / "src")))
        self.assertFalse(in_git_repo(str(self.scratch)))
        self.assertFalse(in_git_repo(""))
        # a `.git` in HOME itself is not a project
        fake_home = self.tmp / "home-dir"
        (fake_home / ".git").mkdir(parents=True)
        (fake_home / "notes").mkdir()
        env = {**self.env, "HOME": str(fake_home)}
        out = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r})\n"
                              f"from brain.recall import in_git_repo; print(in_git_repo({str(fake_home / 'notes')!r}))"],
                             capture_output=True, text=True, env=env)
        self.assertEqual(out.stdout.strip(), "False", out.stderr)

    def test_mute_list_comes_from_config(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"brain": {"enabled": True, "vault": str(self.vault),
                                                                     "code_session_mute": []}}))
        self.reindex()
        code = self.recall(self.PROMPT, "cs-cfg", str(self.repo))
        self.assertIn("Viagem para Trancoso", code)         # nothing muted when the list is empty


class NoCard(IdeasCase):
    """3.5: the catalog stays on disk and in the reindex, but recall injects
    no card from it, in any mode."""

    def test_recall_injects_no_card_even_with_the_catalog_on(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"brain": {"enabled": True, "vault": str(self.vault),
                                                                     "languages": ["pt"],
                                                                     "catalog": {"enabled": True}}}))
        (self.vault / "authority.json").write_text(json.dumps({"entities": [
            {"id": "vehicle-fiat-argo", "type": "vehicle", "canonical": "Fiat Argo", "owner": "user",
             "variants": [], "pages": ["Claude Web/Viagens/Viagem para Trancoso.md"]}]}))
        self.reindex()
        from brain import catalog, core
        cfg = core.load_config()
        cfg["vault"] = self.vault
        # the resolver still works, offline
        self.assertEqual([e["id"] for e, _, _ in catalog.resolve("qual é o meu carro?", cfg)], ["vehicle-fiat-argo"])
        # but "meu carro" alone brings nothing into the session any more
        self.assertIsNone(self.recall("qual é o meu carro, lembra? preciso do modelo", "nc1", str(self.scratch)))
        rows = self.log_rows()
        self.assertTrue(all(all(s < 100 for _, s in r["picked"]) for r in rows), rows)


class IndexDiet(IdeasCase):
    """3.5: the raw session log and the proposals page are not searchable;
    stored questions only enter on request."""

    def test_raw_sessions_and_proposals_page_stay_out_of_the_index(self):
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"brain": {
            "enabled": True, "vault": str(self.vault), "raw_dir": "Sessions/Raw",
            "proposals_page": "Concepts/Harness Proposals.md", "truth_page": "Known Truths.md"}}))
        (self.vault / "Sessions" / "Raw").mkdir(parents=True)
        (self.vault / "Sessions" / "Raw" / "2026-11-08.md").write_text(
            "# Sessions of 2026-11-08\n\n## 10:00 billing-api\n\nexponential backoff for the invoice worker, " * 5)
        (self.vault / "Sessions" / "Distilled note.md").write_text(
            "# Distilled note\n\nThe invoice worker uses exponential backoff with jitter. " * 5)
        (self.vault / "Concepts").mkdir()
        (self.vault / "Concepts" / "Harness Proposals.md").write_text(
            "# Harness Proposals\n\n- [ ] `st-1` **stopword** `backoff` — exponential backoff invoice worker " * 5)
        (self.vault / "Known Truths.md").write_text("# Known Truths\n\n| exponential backoff invoice worker | x |\n" * 5)
        self.reindex()
        ids = {json.loads(l)["id"] for l in (self.home / "brain" / "ksearch-index.jsonl").read_text().splitlines()}
        self.assertIn("Projects/billing-api.md", ids)
        self.assertIn("Sessions/Distilled note.md", ids)
        self.assertNotIn("Sessions/Raw/2026-11-08.md", ids)
        self.assertNotIn("Concepts/Harness Proposals.md", ids)
        self.assertNotIn("Known Truths.md", ids)

    def test_stored_questions_enter_only_with_the_flag(self):
        self.home.mkdir(parents=True)
        (self.home / "brain").mkdir()
        (self.home / "brain" / "questions.jsonl").write_text(json.dumps(
            {"unit": "Projects/billing-api.md#0", "doc": "Projects/billing-api.md", "hash": "x",
             "questions": ["Quando as faturas sao geradas?"], "ts": 1}) + "\n")

        def q_turns():
            return sum(1 for l in (self.home / "brain" / "ksearch-index.jsonl").read_text().splitlines()
                       for t in json.loads(l)["turns"] if t["r"] == "q")
        self.reindex()
        self.assertEqual(q_turns(), 0)
        self.reindex("--with-questions")
        self.assertEqual(q_turns(), 1)
        self.reindex()                                    # the automatic path: back to none
        self.assertEqual(q_turns(), 0)


class SessionStartContext(IdeasCase):
    """3.5: no "when to use" block; a folder over its limit shows the most
    recently modified pages."""

    def test_no_when_to_use_block_and_limit_keeps_recent_pages(self):
        for i in range(5):
            p = self.vault / "Projects" / f"proj-{i}.md"
            p.write_text(f"# proj-{i}\n\nSomething about project {i}.\n")
            os.utime(p, (time.time() - (10 - i) * 86400,) * 2)       # proj-4 newest, proj-0 oldest
        old = self.vault / "Projects" / "billing-api.md"
        os.utime(old, (time.time() - 100 * 86400,) * 2)
        self.home.mkdir(parents=True)
        (self.home / "config.json").write_text(json.dumps({"brain": {
            "enabled": True, "vault": str(self.vault), "languages": ["pt"],
            "context_sections": [{"title": "Projetos", "path": "Projects", "depth": 2, "limit": 3}]}}))
        ctx = self.context(self.run_py("vault_context.py", {}))
        self.assertNotIn("Quando usar", ctx)
        self.assertNotIn("When to use", ctx)
        self.assertNotIn("AO FIM", ctx)
        listed = [l[2:] for l in ctx.splitlines() if l.startswith("- ")]
        self.assertEqual(listed, ["proj-2", "proj-3", "proj-4"])
        self.assertNotIn("billing-api", listed)


class HitRateLog(IdeasCase):
    """The measurement reads the decision log: skipped, silent, injected,
    and the injected pages by vault folder."""

    def test_hit_rate_reports_the_log(self):
        self.reindex()
        self.recall("<task-notification>x", "hr1", str(self.repo))
        self.recall("billing-api invoice worker backoff cobra a reserva da viagem para trancoso", "hr2", str(self.scratch))
        out = self.run_py("hit_rate.py")
        self.assertIn("RECALL LOG (2 decisions)", out)
        self.assertIn("skip:non-human              1", out)
        self.assertIn("inject                      1", out)
        self.assertIn("Claude Web/", out)
        self.assertIn("mode                   chat 1", out)


if __name__ == "__main__":
    unittest.main()
