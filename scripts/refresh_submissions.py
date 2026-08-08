#!/usr/bin/env python3
"""Rebuild every open submission branch on top of current main.

Why this exists: a submission branch is cut from main when its checks run, and
every listing used to append to the end of one apps.yml, so the second
submission to arrive conflicted with the first the moment the first was merged.
That cause is gone -- each app is its own file under apps/ now, and two
submissions cannot touch the same bytes.

This remains useful for the other reason a branch goes stale: a submission whose
own repo has cut a new release since the checks ran, so the entry would be built
from information that has since moved.

Rebuilding rather than merging is the point. The validator is the only thing
that has ever written these entries; re-running it against current main repeats
what already happened instead of reconciling two guesses about a text file.

Safe to run repeatedly: a branch whose content would not change is left alone,
so this doesn't churn PRs on every push.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = os.environ.get("GITHUB_REPOSITORY", "gi-os/brightmarket-index")
TOKEN = os.environ["GH_TOKEN"]
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "User-Agent": "brightmarket-refresh",
}


def get(path):
    req = urllib.request.Request(f"{API}{path}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def run(*args, check=True):
    return subprocess.run(args, check=check, capture_output=True, text=True)


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    prs = [
        p for p in get(f"/repos/{REPO}/pulls?state=open&per_page=100")
        # Only branches this project's own workflow creates.
        if p["head"]["ref"].startswith("submit/")
    ]
    if not prs:
        print("no open submissions")
        return 0

    run("git", "config", "user.name", "brightmarket-bot")
    run("git", "config", "user.email", "bot@users.noreply.github.com")
    base = run("git", "rev-parse", "--short", "origin/main").stdout.strip()

    for pr in prs:
        num, branch = pr["number"], pr["head"]["ref"]
        m = re.search(r"Closes #(\d+)", pr.get("body") or "")
        if not m:
            print(f"PR #{num}: no linked issue, leaving alone")
            continue
        issue_no = m.group(1)

        print(f"--- PR #{num} ({branch}) from issue #{issue_no}")
        run("git", "checkout", "-B", f"refresh-{num}", "origin/main")

        issue = get(f"/repos/{REPO}/issues/{issue_no}")
        env = dict(os.environ, ISSUE_BODY=issue.get("body") or "")
        # GITHUB_OUTPUT would otherwise be appended to with a second status,
        # confusing whatever reads it next.
        env.pop("GITHUB_OUTPUT", None)
        result = subprocess.run(
            [sys.executable, os.path.join(root, "scripts", "validate_submission.py")],
            env=env, capture_output=True, text=True,
        )
        print(result.stdout.strip() or result.stderr.strip())

        # Stage first, then ask. `git diff` reports only tracked files, and a new
        # listing is now a NEW file under apps/ -- so the check said "nothing
        # changed" about a file that had just been created, and every rebuild
        # quietly did nothing. It was only ever right back when every app lived
        # in one already-tracked apps.yml.
        run("git", "add", "-A", "apps")
        if run("git", "diff", "--cached", "--quiet", "--", "apps", check=False).returncode == 0:
            # Either the validator refused, or the app is already in main by
            # another route. Both mean: don't touch the branch.
            print(f"PR #{num}: nothing to add on top of main, leaving it")
            run("git", "checkout", "--force", "main")
            continue
        run("git", "commit", "-m", f"index: submit from #{issue_no} (rebuilt on {base})")
        run("git", "push", "--force", "origin", f"refresh-{num}:{branch}")
        print(f"PR #{num}: refreshed onto {base}")
        run("git", "checkout", "--force", "main")

    return 0


if __name__ == "__main__":
    sys.exit(main())
