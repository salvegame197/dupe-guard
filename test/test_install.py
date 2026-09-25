"""
The hook-only install path, against a throwaway HOME: install registers what
hooks.json declares, running it again syncs instead of duplicating, it refuses
when another guard or the plugin is active, and uninstall removes only what
it added.
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
FOREIGN = {"matcher": "*", "hooks": [{"type": "command", "command": "/usr/bin/true my-own-hook"}]}


def declared():
    spec = json.loads((ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    return sum(len(r["hooks"]) for rules in spec.values() for r in rules)


class Install(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="dupe-guard-install-"))
        (self.home / ".claude").mkdir()
        self.settings = self.home / ".claude" / "settings.json"
        self.settings.write_text(json.dumps({"model": "x", "hooks": {"Stop": [FOREIGN]}}))
        self.env = {**os.environ, "HOME": str(self.home), "DUPE_GUARD_HOME": str(self.home / ".dupe-guard")}

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    def run_sh(self, name):
        return subprocess.run(["bash", str(ROOT / name)], capture_output=True, text=True,
                              env=self.env, timeout=30)

    def ours(self):
        data = json.loads(self.settings.read_text())
        return [h["command"] for rules in data.get("hooks", {}).values()
                for r in rules for h in r["hooks"] if str(ROOT) in h["command"]]

    def test_install_registers_everything_hooks_json_declares(self):
        r = self.run_sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cmds = self.ours()
        self.assertEqual(len(cmds), declared())
        self.assertTrue(any("guard.py" in c for c in cmds))
        self.assertTrue(any("brain/session_log.py" in c for c in cmds))
        # an absolute interpreter that exists (whichever python ran install.sh)
        for c in cmds:
            interp = c.split('"')[1]
            self.assertTrue(os.path.isabs(interp) and os.path.exists(interp), c)
        self.assertNotIn("${CLAUDE_PLUGIN_ROOT}", " ".join(cmds))
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["model"], "x")                           # rest untouched
        self.assertIn(FOREIGN, data["hooks"]["Stop"])
        self.assertIn("registered but off", r.stdout)

    def test_running_it_again_syncs_without_duplicating(self):
        self.run_sh("install.sh")
        r = self.run_sh("install.sh")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("already up to date", r.stdout)
        self.assertEqual(len(self.ours()), declared())

    def test_an_update_that_adds_a_hook_is_picked_up(self):
        # Simulate an older install that only had the guard (what install.sh
        # used to register) and run the current installer over it.
        g = f'"{sys.executable}" "{ROOT}/guard.py"'
        self.settings.write_text(json.dumps({"hooks": {
            "PreToolUse": [{"matcher": "Write|Edit|MultiEdit", "hooks": [{"type": "command", "command": g}]}],
            "Stop": [FOREIGN]}}))
        r = self.run_sh("install.sh")
        self.assertIn("updated", r.stdout)
        self.assertEqual(len(self.ours()), declared())

    def test_refuses_when_another_guard_is_registered(self):
        other = {"matcher": "Write", "hooks": [{"type": "command", "command": "python3 /elsewhere/guard.py"}]}
        self.settings.write_text(json.dumps({"hooks": {"PreToolUse": [other]}}))
        before = self.settings.read_text()
        r = self.run_sh("install.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("another path", r.stdout)
        self.assertEqual(self.settings.read_text(), before)

    def test_refuses_when_the_plugin_is_enabled(self):
        self.settings.write_text(json.dumps({"enabledPlugins": {"dupe-guard@dupe-guard": True}}))
        r = self.run_sh("install.sh")
        self.assertEqual(r.returncode, 1)
        self.assertIn("plugin is enabled", r.stdout)

    def test_uninstall_removes_only_what_it_added_and_deletes_no_data(self):
        self.run_sh("install.sh")
        data_dir = self.home / ".dupe-guard"
        data_dir.mkdir()
        (data_dir / "config.json").write_text('{"brain": {"enabled": true, "vault": "/somewhere/notes"}}')
        r = self.run_sh("uninstall.sh")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(self.ours(), [])
        data = json.loads(self.settings.read_text())
        self.assertEqual(data["hooks"], {"Stop": [FOREIGN]})
        self.assertTrue((data_dir / "config.json").exists())
        self.assertIn("/somewhere/notes", r.stdout)                  # tells where the notes are
        self.assertTrue(list((self.home / ".claude").glob("settings.json.bkp-*")))

    def test_uninstall_with_nothing_installed_is_a_no_op(self):
        before = self.settings.read_text()
        self.assertIn("nothing to do", self.run_sh("uninstall.sh").stdout)
        self.assertEqual(self.settings.read_text(), before)


if __name__ == "__main__":
    unittest.main()
