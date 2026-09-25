#!/usr/bin/env python3
"""
Cut a release: bump the version in both manifests, run the tests, commit,
and tag with `claude plugin tag`.

`/plugin update` only moves users forward when the version changes. Code
pushed without a bump never reaches anyone, and nothing says so: the update
answers "already at the latest version". This makes the bump the easy path.

Usage:
    release.py 0.4.0            bump, test, commit, tag
    release.py 0.4.0 --push     ...and push the commit and the tag
"""
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKET = ROOT / ".claude-plugin" / "marketplace.json"


def run(*cmd, check=True):
    print("$", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT, check=check)


def main() -> int:
    if len(sys.argv) < 2 or not re.fullmatch(r"\d+\.\d+\.\d+", sys.argv[1]):
        print(__doc__)
        return 2
    new, push = sys.argv[1], "--push" in sys.argv
    if subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True,
                      text=True).stdout.strip():
        print("the working tree is dirty: commit or stash first.")
        return 1
    plugin = json.loads(PLUGIN.read_text())
    old = plugin["version"]
    if tuple(map(int, new.split("."))) <= tuple(map(int, old.split("."))):
        print(f"{new} is not above the current {old}.")
        return 1

    plugin["version"] = new
    PLUGIN.write_text(json.dumps(plugin, indent=2) + "\n")
    market = json.loads(MARKET.read_text())
    for p in market["plugins"]:
        if p["name"] == plugin["name"]:
            p["version"] = new
    MARKET.write_text(json.dumps(market, indent=2) + "\n")

    if run(sys.executable, "-m", "unittest", "discover", "-s", "test", check=False).returncode:
        run("git", "checkout", "--", str(PLUGIN), str(MARKET))
        print("tests failed: version left at", old)
        return 1
    run("git", "commit", "-qam", f"Release {new}")
    run("claude", "plugin", "tag", ".", "-m", "dupe-guard %s")
    if push:
        run("git", "push")
        run("git", "push", "origin", f"{plugin['name']}--v{new}")
    print(f"\nreleased {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
