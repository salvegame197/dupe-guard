"""
Tests for piece 3 of the 2026-11-08 plan (docs/IDEIAS-MYTHOS-2026-11-08.md,
items 3.2 and 3.3): the native memory Claude Code keeps per project folder
is indexed and recalled from OTHER folders only; a session that already
wrote a memory note or a CLAUDE.md is not queued for the distiller; hit_rate
measures outcomes (lead touched, candidate edited, correction vs base rate,
ksearch, rule written) and the strength table is built from them.

Everything runs in a throwaway DUPE_GUARD_HOME with its own vault, repo and
transcripts folder.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BRAIN = ROOT / "brain"
sys.path.insert(0, str(ROOT))

from brain.core import project_folder_name          # noqa: E402

NOTE = """# billing-api

## Architecture

Invoices are generated nightly by the invoice worker; retries use exponential backoff.
"""
FEEDBACK = """---
name: never-reset-turnstile
description: Never reset the turnstile counter in gate-control; the gate hardware keeps it
metadata:
  node_type: memory
  type: feedback
  modified: 2026-10-12T10:00:00.000Z
---

Never call `resetTurnstile()` on the gate: the hardware counter is the source
of truth and the user said so twice. Use `syncTurnstile()` from `src/gate.ts`.
"""
PROJECT = """---
name: turnstile-sync-plan
description: Plan for the turnstile sync job in gate-control
metadata:
  node_type: memory
  type: project
---

Never reset the turnstile counter; the sync job reads it and reconciles the
gate hardware once a minute, so a reset would drift the whole day.
"""


def line(**row):
    return json.dumps(row, ensure_ascii=False) + "\n"


class Piece3Case(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dupe-guard-p3-"))
        self.home = self.tmp / "home"
        self.vault = self.tmp / "vault"
        self.repo = self.tmp / "billing-api"
        self.other = self.tmp / "gate-control"
        self.transcripts = self.tmp / "transcripts"
        (self.vault / "Projects").mkdir(parents=True)
        (self.vault / "Projects" / "billing-api.md").write_text(NOTE)
        for r in (self.repo, self.other):
            (r / "src").mkdir(parents=True)
            (r / "src" / "invoice.py").write_text("def total(): return 1\n")
            subprocess.run(["git", "init", "-q"], cwd=r, check=True)
        self.transcripts.mkdir()
        self.env = {**os.environ, "DUPE_GUARD_HOME": str(self.home), "DUPE_GUARD_VAULT": str(self.vault),
                    "DUPE_GUARD_TRANSCRIPTS": str(self.transcripts), "DUPE_GUARD_BRAIN": "1"}
        self.env.pop("DUPE_GUARD_DISABLE", None)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_py(self, script, payload=None, args=(), env=None, cwd=None):
        r = subprocess.run([sys.executable, str(BRAIN / script), *args],
                           input=json.dumps(payload) if payload is not None else "",
                           capture_output=True, text=True, timeout=60, env=env or self.env, cwd=cwd)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)
        return r.stdout

    def context(self, out):
        return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else None

    def reindex(self):
        return subprocess.run([sys.executable, str(BRAIN / "ksearch.py"), "--reindex"],
                              capture_output=True, text=True, env=self.env).stderr

    def index(self):
        return [json.loads(l) for l in (self.home / "brain" / "ksearch-index.jsonl").read_text().splitlines()]

    def memory_folder(self, cwd, with_transcript=True):
        """~/.claude/projects/<folder>/memory for a cwd, the way Claude Code
        keys it, with one tiny transcript so the index learns the cwd."""
        folder = self.transcripts / project_folder_name(str(cwd))
        (folder / "memory").mkdir(parents=True)
        if with_transcript:
            (folder / "t1.jsonl").write_text(
                line(type="user", cwd=str(cwd), timestamp="2026-10-01T10:00:00Z",
                     message={"role": "user", "content": "hello there, anything about invoices today?"})
                + line(type="assistant", cwd=str(cwd), timestamp="2026-10-01T10:00:30Z",
                       message={"role": "assistant", "content": [{"type": "text",
                                "text": "Nothing on invoices today beyond the nightly worker run."}]}))
        return folder / "memory"

    def recall(self, prompt, sid, cwd):
        return self.context(self.run_py("recall.py", {"prompt": prompt, "session_id": sid, "cwd": cwd}))


class MemoryIndex(Piece3Case):
    """3.2: memory/*.md becomes documents; MEMORY.md and the worker do not."""

    def test_memory_notes_are_indexed_with_frontmatter_and_the_index_file_is_not(self):
        mem = self.memory_folder(self.other)
        (mem / "never-reset-turnstile.md").write_text(FEEDBACK)
        (mem / "turnstile-sync-plan.md").write_text(PROJECT)
        (mem / "MEMORY.md").write_text("# Memory index\n\n- [never-reset-turnstile](never-reset-turnstile.md) rule\n" * 5)
        worker = self.transcripts / project_folder_name(self.home / "worker") / "memory"
        worker.mkdir(parents=True)
        (worker / "leak.md").write_text("---\nname: leak\ndescription: the brain talking to itself\n---\n\n" + "x " * 60)
        err = self.reindex()
        self.assertIn("2 native memory notes", err)
        docs = {d["id"]: d for d in self.index() if d["src"] == "memory"}
        folder = project_folder_name(str(self.other))
        self.assertEqual(set(docs), {f"memory/{folder}/never-reset-turnstile.md",
                                     f"memory/{folder}/turnstile-sync-plan.md"})
        fb = docs[f"memory/{folder}/never-reset-turnstile.md"]
        self.assertEqual(fb["title"], "never-reset-turnstile")
        self.assertEqual(fb["kind"], "feedback")
        self.assertEqual(fb["project"], "gate-control")       # from the transcript's cwd
        self.assertEqual(fb["date"], "2026-10-12")              # metadata.modified, not mtime
        self.assertTrue(fb["desc"].startswith("Never reset the turnstile"))
        self.assertIn("turnstile", fb["tf"])                    # description words count as title
        self.assertEqual(docs[f"memory/{folder}/turnstile-sync-plan.md"]["kind"], "project")
        self.assertFalse(any("MEMORY.md" in i or "leak" in i for i in docs))

    def test_feedback_outranks_project_and_both_outrank_the_vault(self):
        from brain.core import DEFAULTS
        from brain.recall import weighted
        cfg = {"importance": {"vault": 1.2, "Development/": 1.35, "Development/Projects/": 1.5,
                              "transcript": 1.0}}          # a config that never heard of memory
        vault = weighted(10.0, {"src": "vault", "id": "Development/Projects/x.md", "title": "x"}, cfg)
        proj = weighted(10.0, {"src": "memory", "id": "memory/f/p.md", "title": "p", "kind": "project"}, cfg)
        fb = weighted(10.0, {"src": "memory", "id": "memory/f/q.md", "title": "q", "kind": "feedback"}, cfg)
        self.assertGreater(proj, vault)
        self.assertGreater(fb, proj)
        self.assertAlmostEqual(proj, 10.0 * DEFAULTS["importance"]["memory"])

    def test_search_cli_filters_and_marks_memory(self):
        mem = self.memory_folder(self.other)
        (mem / "never-reset-turnstile.md").write_text(FEEDBACK)
        self.reindex()
        out = self.run_py("ksearch.py", args=["turnstile", "--source", "memory"])
        self.assertIn(".M [gate-control]", out)
        self.assertIn("never-reset-turnstile", out)
        out = json.loads(self.run_py("ksearch.py", args=["turnstile", "--source", "vault", "--json"]))
        self.assertEqual(out["matched"], 0)


class MemoryRecall(Piece3Case):
    """3.2: the memory of the session's own folder is never injected; another
    folder's is, labelled with where it came from."""

    def setUp(self):
        super().setUp()
        mem = self.memory_folder(self.other)
        (mem / "never-reset-turnstile.md").write_text(FEEDBACK)
        self.reindex()

    def test_own_folder_memory_is_skipped_and_other_folders_are_labelled(self):
        prompt = "should I reset the turnstile counter on the gate before syncing the hardware?"
        self.assertIsNone(self.recall(prompt, "own1", str(self.other)))
        ctx = self.recall(prompt, "oth1", str(self.repo))
        self.assertIsNotNone(ctx)
        self.assertIn("**never-reset-turnstile** (memory of gate-control, 2026-10-12)", ctx)
        self.assertIn("resetTurnstile", ctx)
        rows = [json.loads(l) for l in (self.home / "brain" / "recall-log.jsonl").read_text().splitlines()]
        own, other = rows[0], rows[1]
        self.assertEqual(own["decision"], "silent")
        self.assertEqual(other["decision"], "inject")
        self.assertEqual(other["repo"], "billing-api")
        self.assertTrue(other["ids"][0].startswith("memory/"))

    def test_a_subfolder_of_the_repo_has_its_own_memory_folder(self):
        from brain.recall import own_memory_prefix
        self.assertEqual(own_memory_prefix(str(self.other)), f"memory/{project_folder_name(str(self.other))}/")
        self.assertNotEqual(own_memory_prefix(str(self.other / "src")), own_memory_prefix(str(self.other)))
        self.assertEqual(own_memory_prefix(""), "")

    def test_portuguese_label(self):
        (self.home / "config.json").write_text(json.dumps({"brain": {"languages": ["pt"]}}))
        ctx = self.recall("devo zerar o contador da turnstile antes de sincronizar o hardware?", "pt1", str(self.repo))
        self.assertIsNotNone(ctx)
        self.assertIn("(memoria de gate-control, 2026-10-12)", ctx)


class NoDistillAfterNativeWrite(Piece3Case):
    """3.2: a session that wrote memory/*.md or CLAUDE.md is logged but not
    queued for the distiller; the reason is in the queue row."""

    def transcript(self, sid, edits):
        rows = [line(type="ai-title", aiTitle="Invoice retries")]
        prompts = ["make invoice retries use exponential backoff please",
                   "keep the nightly job, do not move invoices to a queue",
                   "and write down why we kept the nightly job"]
        for t, p in enumerate(prompts, 1):
            rows.append(line(type="user", cwd=str(self.repo), sessionId=sid, timestamp=f"2026-09-20T10:{t:02d}:00Z",
                             message={"role": "user", "content": p}))
            content = [{"type": "text", "text": "Decided to keep the nightly job because the queue doubles failure modes. " * 2}]
            content += [{"type": "tool_use", "id": f"t{t}{i}", "name": "Edit", "input": {"file_path": e}}
                        for i, e in enumerate(edits if t == 2 else [f"{self.repo}/src/invoice.py"])]
            rows.append(line(type="assistant", cwd=str(self.repo), timestamp=f"2026-09-20T10:{t:02d}:30Z",
                             message={"role": "assistant", "usage": {"input_tokens": 10, "output_tokens": 5},
                                      "content": content}))
        tp = self.tmp / f"{sid}.jsonl"
        tp.write_text("".join(rows))
        return tp

    def end(self, sid, edits):
        tp = self.transcript(sid, edits)
        self.run_py("session_log.py", {"session_id": sid, "transcript_path": str(tp), "reason": "exit"})

    def queue_rows(self):
        p = self.home / "brain" / "distill-queue.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_memory_or_claude_md_written_skips_the_queue_with_a_reason(self):
        from brain.core import load_config
        from brain.distill import queue
        memory = f"{self.transcripts}/{project_folder_name(str(self.repo))}/memory/never-reset.md"
        self.end("s-mem", [memory, f"{self.repo}/src/invoice.py"])
        self.end("s-claude", [f"{self.repo}/CLAUDE.md"])
        self.end("s-plain", [f"{self.repo}/src/other.py"])
        rows = {r["session_id"]: r for r in self.queue_rows()}
        self.assertEqual(set(rows), {"s-mem", "s-claude", "s-plain"})
        self.assertIn("never-reset.md", rows["s-mem"]["skipped"])
        self.assertIn("CLAUDE.md", rows["s-claude"]["skipped"])
        self.assertNotIn("skipped", rows["s-plain"])
        os.environ.update({k: self.env[k] for k in ("DUPE_GUARD_HOME", "DUPE_GUARD_VAULT", "DUPE_GUARD_TRANSCRIPTS")})
        try:
            import importlib
            import brain.core, brain.distill
            importlib.reload(brain.core)
            importlib.reload(brain.distill)
            pending = [i["session_id"] for i in brain.distill.queue(brain.core.load_config())]
        finally:
            for k in ("DUPE_GUARD_HOME", "DUPE_GUARD_VAULT", "DUPE_GUARD_TRANSCRIPTS"):
                os.environ.pop(k, None)
            importlib.reload(brain.core)
            importlib.reload(brain.distill)
        self.assertEqual(pending, ["s-plain"])
        raw = next((self.vault / "Sessions" / "Raw").glob("*.md")).read_text()
        self.assertIn("Distill skipped: this session already wrote `never-reset.md`", raw)
        self.assertEqual(raw.count("Distill skipped"), 2)

    def test_a_cat_of_claude_md_in_bash_is_not_learning(self):
        from brain.session_log import learned_files
        self.assertEqual(learned_files({"/r/CLAUDE.md", "/r/src/a.py", "/h/.claude/projects/-x/memory/n.md",
                                        "/h/.claude/projects/-x/memory/MEMORY.md", "/r/docs/CLAUDE.md.bak",
                                        "/r/memory/notes.md"}),
                         ["/h/.claude/projects/-x/memory/MEMORY.md", "/h/.claude/projects/-x/memory/n.md", "/r/CLAUDE.md"])
        self.assertEqual(learned_files({"/t/-x/memory/n.md", "/r/memory/n.md"}, Path("/t")), ["/t/-x/memory/n.md"])


RECALL_TEXT = ("## Accumulated knowledge on this subject\n\nAutomatic search for: invoice — 3 result(s), the strongest below.\n\n"
               "**billing-api** (vault, 2026-09-01)\n"
               "  - retries use `retry_backoff()` in `src/invoice.py:3`; config key `production` is read at start\n\n"
               "**Exponential backoff** (vault, 2026-09-01)\n"
               "  - Retry delays double each attempt, with jitter\n\n"
               "_A reminder._ `python3 /x/ksearch.py <terms>`")
GUARD_TEXT = ("## Reuse: possible duplication\n\n**`handleCreditError`** already exists at `src/old.js:2`\n")


class Outcomes(Piece3Case):
    """3.3: every signal on a synthetic transcript."""

    def write(self, sid, rows):
        folder = self.transcripts / project_folder_name(str(self.repo))
        folder.mkdir(exist_ok=True)
        tp = folder / f"{sid}.jsonl"
        tp.write_text("".join(rows))
        return tp

    def user(self, text, ts="2026-09-20T10:00:00Z"):
        return line(type="user", cwd=str(self.repo), timestamp=ts, message={"role": "user", "content": text})

    def recall_row(self, text=RECALL_TEXT, ts="2026-09-20T10:00:01Z"):
        return line(type="attachment", timestamp=ts, attachment={"type": "hook_additional_context",
                                                                 "hookName": "UserPromptSubmit", "content": [text]})

    def assistant(self, tools=(), text="", ts="2026-09-20T10:00:10Z"):
        content = ([{"type": "text", "text": text}] if text else []) + [
            {"type": "tool_use", "id": f"t{i}", "name": name, "input": inp} for i, (name, inp) in enumerate(tools)]
        return line(type="assistant", cwd=str(self.repo), timestamp=ts, message={"role": "assistant", "content": content})

    def measure(self, tp):
        return json.loads(self.run_py("hit_rate.py", args=["--json", str(tp)]))

    def test_lead_touched_by_read_edit_bash_or_grep(self):
        for tool, inp in (("Read", {"file_path": f"{self.repo}/src/invoice.py"}),
                          ("Edit", {"file_path": f"{self.repo}/src/x.py", "old_string": "a", "new_string": "retry_backoff()"}),
                          ("Bash", {"command": "grep -n retry_backoff src/*.py"}),
                          ("Grep", {"pattern": "retry_backoff", "path": "src"})):
            tp = self.write("lead-" + tool, [self.user("how do invoice retries work here, exactly?"), self.recall_row(),
                                              self.assistant([(tool, inp)])])
            r = self.measure(tp)["recall"]
            self.assertEqual((r["injections"], r["with_lead"], r["touched"]), (1, 1, 1), tool)

    def test_plain_words_and_the_repo_name_are_not_leads(self):
        from brain.hit_rate import leads_in, touched
        self.assertEqual(leads_in("key `production` is read; see `src/invoice.py:3` and `retry_backoff()` and `null`"),
                         ["src/invoice.py:3", "retry_backoff"])
        tools = [{"args": "cd /r/billing-api && cat config/production"}]
        self.assertFalse(touched(["config/production"], tools, "/r/billing-api"))
        self.assertFalse(touched(["billing-api"], tools, "/r/billing-api"))
        self.assertTrue(touched(["src/invoice.py:3"], [{"args": "/r/billing-api/src/invoice.py"}], "/r/billing-api"))
        tp = self.write("lead-no", [self.user("how do invoice retries work here, exactly?"), self.recall_row(),
                                    self.assistant([("Bash", {"command": "cat config/production"})])])
        r = self.measure(tp)["recall"]
        self.assertEqual((r["with_lead"], r["touched"]), (1, 0))

    def test_correction_in_the_next_human_prompt_against_the_base_rate(self):
        rows = [self.user("how do invoice retries work here, exactly?"), self.recall_row(),
                self.assistant(text="They use exponential backoff in the invoice worker."),
                self.user("<task-notification>done</task-notification>", "2026-09-20T10:01:00Z"),
                self.user("no, that is wrong", "2026-09-20T10:02:00Z"),              # short, still human
                self.assistant(text="You are right.", ts="2026-09-20T10:02:10Z"),
                self.user("ok now please add jitter to the retry delays", "2026-09-20T10:03:00Z"),
                self.assistant(text="Done.", ts="2026-09-20T10:03:10Z")]
        r = self.measure(self.write("corr", rows))
        self.assertEqual(r["recall"]["corrected"], 1)
        self.assertEqual(r["recall"]["with_next"], 1)
        self.assertEqual((r["recall"]["human_corrections"], r["recall"]["human_prompts"]), (1, 3))
        self.assertAlmostEqual(r["base_rates"]["billing-api"], 1 / 3)
        self.assertEqual(r["pages"]["billing-api"]["corrected"], 1)
        self.assertAlmostEqual(r["pages"]["billing-api"]["expected"], 1 / 3)
        self.assertEqual(r["repos"]["billing-api"]["human"], 3)

    def test_ksearch_rule_written_opened_and_cited(self):
        memory = f"{self.transcripts}/{project_folder_name(str(self.repo))}/memory/jitter.md"
        rows = [self.user("how do invoice retries work here, exactly?"), self.recall_row(),
                self.assistant([("Bash", {"command": "python3 /x/brain/ksearch.py invoice retries"}),
                                ("Read", {"file_path": f"{self.vault}/Projects/billing-api.md"})],
                               text="As the billing-api note says, backoff is exponential."),
                self.user("thanks, remember that", "2026-09-20T10:01:00Z"),
                self.assistant([("Write", {"file_path": memory, "content": "---\nname: jitter\n---\nrule"})],
                               ts="2026-09-20T10:01:10Z")]
        r = self.measure(self.write("sig", rows))["recall"]
        self.assertEqual((r["deeper"], r["learned"], r["opened"], r["cited"]), (1, 1, 1, 1))
        self.assertEqual(r["corrected"], 0)

    def test_guard_candidate_edited_instead_of_a_new_file(self):
        rows = [self.user("add an error handler for debit"),
                self.assistant([("Write", {"file_path": f"{self.repo}/src/new.js", "content": "x"})]),
                line(type="attachment", timestamp="2026-09-20T10:00:11Z",
                     attachment={"type": "hook_additional_context", "hookName": "PreToolUse:Write",
                                 "toolUseID": "t0", "content": [GUARD_TEXT]}),
                self.assistant([("Edit", {"file_path": f"{self.repo}/src/old.js", "old_string": "a", "new_string": "b"})],
                               ts="2026-09-20T10:00:20Z")]
        g = self.measure(self.write("guard", rows))["guard"]
        self.assertEqual((g["warnings"], g["edited_candidate"], g["ignored"]), (1, 1, 0))

    def test_strength_table_moves_with_outcomes_not_with_opened(self):
        from brain.recall import strength_factor
        self.assertEqual(strength_factor("p", {"p": {"injected": 9, "opened": 0, "touched": 0, "corrected": 0, "expected": 0.5}}), 1.0)
        self.assertEqual(strength_factor("p", {"p": {"injected": 9, "touched": 2, "corrected": 0, "expected": 0.5}}), 1.3)
        self.assertEqual(strength_factor("p", {"p": {"injected": 10, "touched": 0, "corrected": 2, "expected": 0.6}}), 0.8)
        self.assertEqual(strength_factor("p", {"p": {"injected": 5, "touched": 0, "corrected": 2, "expected": 0.2}}), 0.6)
        # one correction is an event, not a rate; four injections are too few
        self.assertEqual(strength_factor("p", {"p": {"injected": 5, "touched": 0, "corrected": 1, "expected": 0.0}}), 1.0)
        self.assertEqual(strength_factor("p", {"p": {"injected": 4, "touched": 0, "corrected": 2, "expected": 0.0}}), 1.0)
        # both at once multiply
        self.assertAlmostEqual(strength_factor("p", {"p": {"injected": 10, "touched": 1, "corrected": 2, "expected": 0.6}}), 0.92)
        # end to end: the written table carries the outcome fields and the spread is printed
        self.reindex()
        self.write("st", [self.user("how do invoice retries work here, exactly?"), self.recall_row(),
                          self.assistant([("Read", {"file_path": f"{self.repo}/src/invoice.py"})])])
        out = self.run_py("hit_rate.py", args=["--strength"])
        self.assertIn("1 with a lead touched", out)
        self.assertIn("factor min 1.15, median 1.15, max 1.15; < 0.8: 0, 0.8: 0, 1.0: 0, 1.0-1.3: 1", out)
        table = json.loads((self.home / "brain" / "recall-strength.json").read_text())
        self.assertEqual(table["Projects/billing-api.md"]["touched"], 1)
        self.assertEqual(table["Projects/billing-api.md"]["opened"], 0)

    def test_report_has_the_tables(self):
        self.write("rep", [self.user("how do invoice retries work here, exactly?"), self.recall_row(),
                           self.assistant([("Read", {"file_path": f"{self.repo}/src/invoice.py"})])])
        out = self.run_py("hit_rate.py")
        for mark in ("lead touched", "(a)", "(b)", "(c)", "(d)", "(e)", "BY PAGE", "BY REPO", "billing-api  (1 prompts)"):
            self.assertIn(mark, out)


if __name__ == "__main__":
    unittest.main()


class MemoryDescriptionGate(unittest.TestCase):
    """The name of a native memory opens the recall gate on one term; its
    one-line description, a whole sentence, needs two distinct terms."""

    def rows(self):
        return [
            {"id": "memory/-x-other-repo/proxy-flake.md", "score": 99.0, "source": "memory",
             "title": "proxy-tests-harness-flake", "desc": "proxy.rs reuses a keep-alive client; under stress the test may swap a rare status"},
            {"id": "memory/-x-access/never-reset.md", "score": 98.0, "source": "memory",
             "title": "never-reset-turnstile", "desc": "do not reset the turnstile counter before syncing"},
        ]

    def test_one_description_word_does_not_open_the_gate(self):
        from brain import recall
        picked = recall.pick(self.rows(), ["test", "discount"], set(), "s")
        self.assertEqual([r["id"] for r in picked], [])

    def test_two_description_words_do(self):
        from brain import recall
        picked = recall.pick(self.rows(), ["turnstile", "syncing"], set(), "s")
        self.assertEqual([r["id"] for r in picked], ["memory/-x-access/never-reset.md"])

    def test_one_name_word_still_does(self):
        from brain import recall
        picked = recall.pick(self.rows(), ["flake", "discount"], set(), "s")
        self.assertEqual([r["id"] for r in picked], ["memory/-x-other-repo/proxy-flake.md"])
