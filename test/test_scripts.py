"""
End-to-end tests for the skill scripts and the plugin manifests. Each script
runs as a subprocess against a throwaway git repo, the way a skill calls it.
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
SCRIPTS = ROOT / "scripts"
DEMO = ROOT / "examples" / "demo"

CLONE = (DEMO / "src" / "payments.js").read_text().split("function validateCard")[0] \
    .replace("handleCreditError", "handleDebitError") \
    .replace("// Already in the repo: the body that gets \"rediscovered\" under another name.\n", "")

N_PLUS_ONE = """async function loadTotals(orders) {
    const out = []
    for (const order of orders) {
        const row = await db.query('select sum(total) from items where order_id = ?', [order.id])
        out.push(row)
    }
    return out
}
module.exports = { loadTotals }
"""


def git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class ScriptsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dupe-guard-scripts-"))
        self.home = self.tmp / "home"
        self.repo = self.tmp / "repo"
        shutil.copytree(DEMO, self.repo)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, name, *args):
        r = subprocess.run([sys.executable, str(SCRIPTS / name), *args], cwd=self.repo,
                           capture_output=True, text=True, timeout=60,
                           env={**os.environ, "DUPE_GUARD_HOME": str(self.home)})
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def branch_with(self, path, content):
        git(self.repo, "checkout", "-q", "-b", "feature")
        (self.repo / path).write_text(content)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "feature")

    # --- difflook

    def test_difflook_working_diff_flags_reuse_candidate(self):
        (self.repo / "src" / "debit.js").write_text(CLONE)
        git(self.repo, "add", "-A")          # staged counts as working diff
        out = json.loads(self.run_script("difflook.py", "--json"))
        names = {s["name"] for f in out["files"] for s in f["new"]}
        self.assertIn("handleDebitError", names)

    def test_difflook_base_sees_symbols_new_on_the_branch(self):
        # Regression: --base used HEAD as "before", which is the NEW version,
        # so every new symbol looked pre-existing and the report was always empty.
        self.branch_with("src/debit.js", CLONE)
        out = json.loads(self.run_script("difflook.py", "--base", "main", "--json"))
        names = {s["name"] for f in out["files"] for s in f["new"]}
        self.assertIn("handleDebitError", names)

    def test_difflook_no_changes(self):
        self.assertIn("no changes", self.run_script("difflook.py"))

    # --- smell

    def test_smell_finds_n_plus_one(self):
        (self.repo / "src" / "totals.js").write_text(N_PLUS_ONE)
        out = json.loads(self.run_script("smell.py", "--all", "--json"))
        kinds = {x["kind"] for x in out["findings"]}
        self.assertIn("n+1", kinds)

    def test_smell_clean_repo_says_so(self):
        self.assertIn("no files to analyse", self.run_script("smell.py"))

    # --- propose

    def test_propose_prints_a_draft_and_writes_nothing(self):
        out = self.run_script("propose.py")
        self.assertIn("Proposed conventions", out)
        self.assertIn("DRAFT", out)
        self.assertFalse((self.home / "conventions").exists())


class ManifestTest(unittest.TestCase):
    def test_manifests_are_valid_and_agree(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        entry = next(p for p in market["plugins"] if p["name"] == plugin["name"])
        self.assertEqual(entry["version"], plugin["version"])

    def test_hooks_point_at_files_that_exist(self):
        hooks = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
        for event, rules in hooks.items():
            for rule in rules:
                for h in rule["hooks"]:
                    path = h["command"].split('"')[1].replace("${CLAUDE_PLUGIN_ROOT}", str(ROOT))
                    self.assertTrue(Path(path).exists(), f"{event}: {path}")

    def test_skills_reference_scripts_that_exist(self):
        import re
        for skill in (ROOT / "skills").glob("*/SKILL.md"):
            for ref in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/([\w./-]+)", skill.read_text()):
                self.assertTrue((ROOT / ref).exists(), f"{skill.parent.name}: {ref}")


if __name__ == "__main__":
    unittest.main()
