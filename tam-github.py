#!/usr/bin/env python3
"""
tam-github.py — GitHub activity poller for Tam

Checks cvi-aid for new commits, issues, and PRs since last run.
Prints findings to stdout (one per line), or "NONE" if nothing new.

Usage:
    python3 tam-github.py          # check for activity, print findings
    python3 tam-github.py --reset  # clear stored state (next run is baseline)
"""

import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ───────────────────────────────────────────────────────────────────

TAM_HOME = Path(os.environ.get("TAM_HOME", Path.home() / "tam"))
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "GeorgeLautenschlager/cvi-aid")
STATE_FILE = TAM_HOME / "github_state.json"

# ── GitHub API ───────────────────────────────────────────────────────────────

def gh_get(path: str) -> list | dict:
    url = f"https://api.github.com/{path.lstrip('/')}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "tam-assistant",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())

# ── State ────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ── Polling ──────────────────────────────────────────────────────────────────

def check_repo() -> list[str]:
    findings = []
    state = load_state()
    new_state = dict(state)

    # Commits
    try:
        commits = gh_get(f"repos/{GITHUB_REPO}/commits?per_page=10")
        if commits:
            latest_sha = commits[0]["sha"]
            last_sha = state.get("last_commit_sha")
            if last_sha is None:
                # First run — set baseline, don't surface
                new_state["last_commit_sha"] = latest_sha
            elif latest_sha != last_sha:
                new_commits = []
                for c in commits:
                    if c["sha"] == last_sha:
                        break
                    new_commits.append(c)
                if new_commits:
                    count = len(new_commits)
                    first_msg = new_commits[0]["commit"]["message"].split("\n")[0]
                    suffix = f" (+{count - 1} more)" if count > 1 else ""
                    findings.append(f"{count} new commit(s) on cvi-aid: \"{first_msg}\"{suffix}")
                new_state["last_commit_sha"] = latest_sha
    except Exception as e:
        findings.append(f"[cvi-aid] Commit check failed: {e}")

    # Issues (GitHub issues endpoint includes PRs — filter them out)
    try:
        items = gh_get(f"repos/{GITHUB_REPO}/issues?state=open&per_page=20&sort=created&direction=desc")
        issues = [i for i in items if "pull_request" not in i]
        last_issue = state.get("last_issue_number", 0)
        new_issues = [i for i in issues if i["number"] > last_issue]
        if new_issues:
            if last_issue > 0:
                for issue in new_issues:
                    findings.append(f"New issue on cvi-aid #{issue['number']}: {issue['title']}")
            # Always update watermark
            new_state["last_issue_number"] = max((i["number"] for i in issues), default=last_issue)
        elif issues and last_issue == 0:
            new_state["last_issue_number"] = issues[0]["number"]
    except Exception as e:
        findings.append(f"[cvi-aid] Issue check failed: {e}")

    # Pull requests
    try:
        prs = gh_get(f"repos/{GITHUB_REPO}/pulls?state=open&per_page=20&sort=created&direction=desc")
        last_pr = state.get("last_pr_number", 0)
        new_prs = [p for p in prs if p["number"] > last_pr]
        if new_prs:
            if last_pr > 0:
                for pr in new_prs:
                    findings.append(f"New PR on cvi-aid #{pr['number']}: {pr['title']}")
            new_state["last_pr_number"] = max((p["number"] for p in prs), default=last_pr)
        elif prs and last_pr == 0:
            new_state["last_pr_number"] = prs[0]["number"]
    except Exception as e:
        findings.append(f"[cvi-aid] PR check failed: {e}")

    save_state(new_state)
    return findings

# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--reset" in sys.argv:
        STATE_FILE.unlink(missing_ok=True)
        print("GitHub state reset.")
        sys.exit(0)

    # Load .env if token not already in environment
    if not GITHUB_TOKEN:
        env_file = TAM_HOME / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("GITHUB_TOKEN="):
                    GITHUB_TOKEN = line.split("=", 1)[1].strip().strip('"\'')
                elif line.startswith("GITHUB_REPO="):
                    GITHUB_REPO = line.split("=", 1)[1].strip().strip('"\'')

    if not GITHUB_TOKEN:
        print("ERROR: GITHUB_TOKEN not set", file=sys.stderr)
        sys.exit(1)

    findings = check_repo()
    if findings:
        for f in findings:
            print(f)
    else:
        print("NONE")
