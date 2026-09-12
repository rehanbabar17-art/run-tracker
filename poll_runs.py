#!/usr/bin/env python3
"""Poll GitHub Actions runs of other repos and maintain a daily run log.

The tracker repo never records its own runs. It queries the GitHub API for
the repos listed in repos.json, appends any new workflow runs to run-log.json
(unique by run id), and rewrites TODAY.md with today's summary.

Environment variables
---------------------
NTFY_TOPIC       ntfy topic for notifications (required; set it in the environment)
LAST_RUN_OF_DAY  "true" to trigger end-of-day repo discovery
LAST_RUN_HOUR    PKT hour threshold for auto-discovery fallback (default: 23)
"""

import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPOS_FILE = ROOT / "repos.json"
LOG_FILE = ROOT / "run-log.json"
TODAY_FILE = ROOT / "TODAY.md"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
PER_PAGE = 100
NTFY_TIMEOUT = 10


PKT = ZoneInfo("Asia/Karachi")  # Pakistan Standard Time (UTC+5)


def now_pkt() -> datetime:
    """Current time in Pakistan Standard Time."""
    return datetime.now(PKT)


def tracker_repo() -> str:
    """Return the full name of this tracker repo (owner/repo), or empty string."""
    # In Actions, GITHUB_REPOSITORY is set (e.g. "owner/repo")
    val = os.environ.get("GITHUB_REPOSITORY")
    if val:
        return val
    # Fallback: read git remote
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
        import re
        m = re.search(r"[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
        return m.group(1) if m else ""
    except Exception:
        return ""


# ── ntfy ──────────────────────────────────────────────────────────────────────

def send_ntfy(title: str, message: str) -> bool:
    if not NTFY_TOPIC:
        return False
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    try:
        req = urllib.request.Request(
            url,
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "default", "Tags": "robot,github"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"ntfy error: {e}", file=sys.stderr)
        return False


def pkt_time(iso: str) -> str:
    """Format an ISO timestamp (UTC) as HH:MM in Pakistan Standard Time."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(PKT)
        return dt.strftime("%H:%M")
    except (ValueError, TypeError):
        return (iso or "")[11:16]


# ── gh api ────────────────────────────────────────────────────────────────────

def gh_api(path: str):
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    result = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if result.returncode != 0:
        print(f"  error querying {path}: {result.stderr.strip()}", file=sys.stderr)
        return None
    return json.loads(result.stdout)


# ── file helpers ──────────────────────────────────────────────────────────────

def load_log() -> list:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def save_log(runs: list) -> None:
    LOG_FILE.write_text(json.dumps(runs, indent=2) + "\n")


def load_repos() -> list:
    if REPOS_FILE.exists():
        return json.loads(REPOS_FILE.read_text())
    return []


def save_repos(repos: list) -> None:
    REPOS_FILE.write_text(json.dumps(sorted(repos), indent=2) + "\n")


# ── fetch workflow runs ───────────────────────────────────────────────────────

def fetch_recent_runs(repo: str) -> list:
    data = gh_api(f"repos/{repo}/actions/runs?per_page={PER_PAGE}")
    if not data:
        return []
    return [
        {
            "repo": repo,
            "run_id": run["id"],
            "run_number": run["run_number"],
            "workflow": run.get("name") or run.get("display_title") or "unknown",
            "event": run.get("event", ""),
            "status": run.get("status", ""),
            "conclusion": run.get("conclusion", ""),
            "created_at": run.get("created_at", ""),
            "actor": (run.get("actor") or {}).get("login", ""),
        }
        for run in data.get("workflow_runs", [])
    ]


# ── update TODAY.md ──────────────────────────────────────────────────────────

def update_today(runs: list) -> None:
    today = now_pkt().strftime("%Y-%m-%d")
    todays = sorted(
        [r for r in runs if (r.get("created_at") or "").startswith(today)],
        key=lambda r: r.get("created_at", ""),
    )
    by_repo: dict = {}
    for run in todays:
        by_repo.setdefault(run["repo"], []).append(run)

    lines = [
        "# Today's Runs",
        "",
        f"Updated at: {now_pkt().strftime('%Y-%m-%d %H:%M:%S')} PKT",
        f"Date: {today}",
        "",
        f"**Total runs today: {len(todays)}**",
        "",
        "## By repo",
        "",
    ]
    if by_repo:
        for repo, runs_in_repo in sorted(by_repo.items()):
            lines.append(f"- `{repo}`: {len(runs_in_repo)} run(s)")
    else:
        lines.append("- No runs recorded yet today.")

    lines += ["", "## Run times (PKT)", ""]
    if todays:
        for run in todays:
            label = run.get("workflow") or "unknown workflow"
            lines.append(
                f"- {pkt_time(run['created_at'])} · `{run['repo']}` · {label} · "
                f"{run.get('event') or '?'} · {run.get('conclusion') or run.get('status') or '?'}"
            )
    else:
        lines.append("- No runs recorded yet today.")

    TODAY_FILE.write_text("\n".join(lines) + "\n")


# ── repo discovery ───────────────────────────────────────────────────────────

def discover_repos() -> list:
    """List all owned repos, return any newly added ones (excluding tracker itself)."""
    current = set(load_repos())
    self_repo = tracker_repo()
    data = gh_api("user/repos?per_page=100&affiliation=owner&sort=updated&direction=desc")
    if not data:
        return []
    all_owned = [r["full_name"] for r in data]
    added = [r for r in all_owned if r not in current and r != self_repo]
    if added:
        updated = sorted(current | set(added))
        save_repos(updated)
    return added


# ── main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    repos = load_repos()
    existing = load_log()
    seen = {r["run_id"] for r in existing}
    new_runs = []

    print(f"Polling {len(repos)} repos...")
    for repo in repos:
        for run in fetch_recent_runs(repo):
            if run["run_id"] not in seen:
                seen.add(run["run_id"])
                new_runs.append(run)

    print(f"Found {len(new_runs)} new run(s).")
    existing.extend(new_runs)
    existing.sort(key=lambda r: (r.get("created_at") or "", r["repo"]))
    save_log(existing)
    update_today(existing)

    # ── today summary for ntfy ────────────────────────────────────────────
    today = now_pkt().strftime("%Y-%m-%d")
    todays = sorted(
        [r for r in existing if (r.get("created_at") or "").startswith(today)],
        key=lambda r: r.get("created_at", ""),
    )

    lines = [
        f"Repos tracked: {len(repos)}",
        f"New runs this poll: {len(new_runs)}",
        f"Total runs today: {len(todays)}",
        "",
        "Per repo:",
    ]
    by_repo: dict = {}
    for run in todays:
        by_repo.setdefault(run["repo"], []).append(run)

    if by_repo:
        for repo, runs_in_repo in sorted(by_repo.items()):
            last = runs_in_repo[-1]
            short = repo.split("/")[-1]
            lines.append(
                f"- {short}: {len(runs_in_repo)} run(s) · last {pkt_time(last['created_at'])} PKT · "
                f"{last.get('conclusion') or last.get('status') or '?'}"
            )
    else:
        lines.append("- No runs recorded yet today.")

    # ── last run of day: discover repos ───────────────────────────────────
    explicit_flag = os.environ.get("LAST_RUN_OF_DAY", "").lower() == "true"
    hour_threshold = int(os.environ.get("LAST_RUN_HOUR", "23"))
    auto_hour = now_pkt().hour >= hour_threshold

    if explicit_flag or auto_hour:
        print("Running end-of-day repo discovery...")
        added = discover_repos()
        if added:
            lines.append("")
            lines.append(f"New repos added: {', '.join(added)}")
            print(f"Discovered {len(added)} new repo(s): {', '.join(added)}")
        else:
            lines.append("")
            lines.append("No new repos found.")

    # ── send ntfy ─────────────────────────────────────────────────────────
    title = f"Run Tracker · {today} · {len(todays)} run(s)"
    message = "\n".join(lines)
    if send_ntfy(title, message):
        print(f"ntfy sent to {NTFY_TOPIC}")
    else:
        print("ntfy send failed")


if __name__ == "__main__":
    main()
