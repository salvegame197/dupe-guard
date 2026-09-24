"""
End-to-end tests: run guard.py as Claude Code would (JSON on stdin) against a
throwaway git repo. Stdlib only, so `python3 -m unittest` is the whole runner.
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
GUARD = ROOT / "guard.py"
DEMO = ROOT / "examples" / "demo"

CLONE = """function handleDebitError(error, order) {
    const code = error && error.code ? error.code : 'UNKNOWN'
    logger.warn('payment failed', { code, order: order.id })
    if (code === 'INSUFFICIENT_FUNDS') {
        return { ok: false, reason: 'funds', retry: false }
    }
    if (code === 'TIMEOUT') {
        return { ok: false, reason: 'network', retry: true }
    }
    return { ok: false, reason: 'unknown', retry: true }
}
"""

UNRELATED = """function computeShippingEstimate(weightKg, zone) {
    const base = zone === 'domestic' ? 4.5 : 18.0
    const perKg = zone === 'domestic' ? 1.2 : 3.4
    if (weightKg <= 0) {
        throw new RangeError('weight must be positive')
    }
    return Math.round((base + perKg * weightKg) * 100) / 100
}
"""


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dupe-guard-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        shutil.copytree(DEMO, self.repo)
        # copytree preserves the source mtimes, so whether the demo files look
        # "recent" to the Bash path depended on when the checkout happened.
        # Age everything; only files a test writes itself count as recent.
        old = time.time() - 3600
        for f in self.repo.rglob("*"):
            if f.is_file():
                os.utime(f, (old, old))
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_guard(self, payload, raw=None):
        env = dict(os.environ, DUPE_GUARD_HOME=str(self.home))
        data = raw if raw is not None else json.dumps(payload)
        r = subprocess.run([sys.executable, str(GUARD)], input=data,
                           capture_output=True, text=True, env=env, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        if not r.stdout.strip():
            return None
        return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]

    def payload(self, content, tool="Write", sid="s1", path="src/new.js"):
        return {"tool_name": tool, "session_id": sid, "cwd": str(self.repo),
                "tool_input": {"file_path": str(self.repo / path), "content": content}}

    def test_detects_clone_under_another_name(self):
        out = self.run_guard(self.payload(CLONE))
        self.assertIsNotNone(out)
        self.assertIn("handleDebitError", out)
        self.assertIn("handleCreditError", out)
        self.assertIn("payments.js", out)

    def test_silent_on_unrelated_code(self):
        self.assertIsNone(self.run_guard(self.payload(UNRELATED)))

    def test_silent_on_garbage_input(self):
        self.assertIsNone(self.run_guard(None, raw="this is not json"))
        self.assertIsNone(self.run_guard(None, raw=""))

    def test_ignores_other_tools(self):
        self.assertIsNone(self.run_guard(self.payload(CLONE, tool="Read")))

    def test_does_not_warn_twice_in_a_session(self):
        self.assertIsNotNone(self.run_guard(self.payload(CLONE, sid="same")))
        self.assertIsNone(self.run_guard(self.payload(CLONE, sid="same")))

    def test_silent_outside_a_git_repo(self):
        shutil.rmtree(self.repo / ".git")
        self.assertIsNone(self.run_guard(self.payload(CLONE)))

    # --- PostToolUse:Bash: the code was not knowable before the command ran

    def bash_payload(self, cmd, cwd=None, sid="b1"):
        return {"tool_name": "Bash", "hook_event_name": "PostToolUse", "session_id": sid,
                "cwd": str(cwd or self.repo), "tool_input": {"command": cmd},
                "tool_response": {}}

    def test_bash_new_file_with_clone_is_caught_after_the_fact(self):
        (self.repo / "src" / "debit.js").write_text(CLONE)
        out = self.run_guard(self.bash_payload("cat > src/debit.js <<'EOF'\n...\nEOF"))
        self.assertIsNotNone(out)
        self.assertIn("handleDebitError", out)
        self.assertIn("handleCreditError", out)
        self.assertIn("You just wrote", out)
        self.assertIn("in `src/debit.js`", out)

    def test_bash_appended_clone_to_tracked_file_is_caught(self):
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "base"], cwd=self.repo, check=True)
        f = self.repo / "src" / "payments.js"
        f.write_text(f.read_text() + "\n" + CLONE)
        out = self.run_guard(self.bash_payload("sed -i '' 's/x/y/' src/payments.js"))
        self.assertIsNotNone(out)
        self.assertIn("handleDebitError", out)
        # the file's own pre-existing symbols are not "duplicates" of themselves
        self.assertNotIn("**`validateCard`**", out)

    def test_bash_that_changed_nothing_is_silent(self):
        self.assertIsNone(self.run_guard(self.bash_payload("git status && ls")))

    def test_bash_cd_into_repo_from_elsewhere(self):
        (self.repo / "src" / "debit.js").write_text(CLONE)
        out = self.run_guard(self.bash_payload(f"cd {self.repo} && cat > src/debit.js <<'EOF'\nEOF",
                                               cwd=self.tmp))
        self.assertIsNotNone(out)
        self.assertIn("handleDebitError", out)

    def test_bash_ignores_stale_changes(self):
        f = self.repo / "src" / "debit.js"
        f.write_text(CLONE)
        old = time.time() - 3600
        os.utime(f, (old, old))
        self.assertIsNone(self.run_guard(self.bash_payload("echo hi")))

    def test_prunes_old_session_state(self):
        state = self.home / "state"
        state.mkdir(parents=True)
        old = state / "ancient.json"
        old.write_text("{}")
        stale = time.time() - 30 * 86400
        os.utime(old, (stale, stale))
        self.run_guard(self.payload(CLONE))
        self.assertFalse(old.exists())
        self.assertTrue((state / "s1.json").exists())


if __name__ == "__main__":
    unittest.main()
