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

FAILED_CONCLUSIONS = {"failure", "timed_out", "cancelled"}

PKT = ZoneInfo("Asia/Karachi")  # Pakistan Standard Time (UTC+5)


def now_pkt() -> datetime:
    """Current time in Pakistan Standard Time."""
    return datetime.now(PKT)


def tracker_repo() -> str:
    """Return the full name of this tracker repo (owner/repo), or empty string."""
    val = os.environ.get("GITHUB_REPOSITORY")
    if val:
        return val
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


def pkt_date(iso: str) -> str:
    """Return the Pakistan Standard Time date (YYYY-MM-DD) of a UTC timestamp."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(PKT)
        return dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return (iso or "")[:10]


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


def is_failed(run: dict) -> bool:
    """Return True if a run concluded with failure/timed_out/cancelled."""
    return (run.get("conclusion") or run.get("status") or "") in FAILED_CONCLUSIONS


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

def update_today(runs: list, tracked_repos: list) -> None:
    today = now_pkt().strftime("%Y-%m-%d")
    todays = sorted(
        [r for r in runs if pkt_date(r.get("created_at", "")) == today],
        key=lambda r: r.get("created_at", ""),
    )
    failed_todays = [r for r in todays if is_failed(r)]
    failed_by_repo: dict[str, list] = {}
    for r in failed_todays:
        failed_by_repo.setdefault(r["repo"], []).append(r)

    by_repo: dict[str, list] = {}
    for run in todays:
        by_repo.setdefault(run["repo"], []).append(run)

    total_failed = len(failed_todays)
    lines = [
        "# Today's Runs",
        "",
        f"Updated at: {now_pkt().strftime('%Y-%m-%d %H:%M:%S')} PKT",
        f"Date: {today}",
        "",
        f"**Total runs today: {len(todays)}**" + (f" ({total_failed} failed)" if total_failed else ""),
        "",
        "## By repo",
        "",
    ]
    for repo in sorted(tracked_repos):
        runs_in_repo = by_repo.get(repo, [])
        if runs_in_repo:
            fails = sum(1 for r in runs_in_repo if is_failed(r))
            tag = f" ({fails} failed)" if fails else ""
            lines.append(f"- `{repo}`: {len(runs_in_repo)} run(s){tag}")
        else:
            lines.append(f"- `{repo}`: 0 run(s) · not run today")

    # ── failed runs detail ────────────────────────────────────────────────
    if failed_todays:
        lines += ["", "## Failures", ""]
        for repo in sorted(failed_by_repo):
            short = repo.split("/")[-1]
            for r in failed_by_repo[repo]:
                label = r.get("workflow") or "unknown workflow"
                run_url = f"https://github.com/{repo}/actions/runs/{r['run_id']}"
                lines.append(
                    f"- ❌ {pkt_time(r['created_at'])} PKT · `{short}` · {label} · "
                    f"{r.get('event') or '?'} · {r.get('conclusion') or r.get('status') or '?'} · "
                    f"[view run]({run_url})"
                )

    # ── all run times ─────────────────────────────────────────────────────
    lines += ["", "## Run times (PKT)", ""]
    if todays:
        for run in todays:
            label = run.get("workflow") or "unknown workflow"
            tag = " ❌" if is_failed(run) else ""
            lines.append(
                f"- {pkt_time(run['created_at'])} · `{run['repo']}` · {label} · "
                f"{run.get('event') or '?'} · {run.get('conclusion') or run.get('status') or '?'}{tag}"
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
    by_id = {r["run_id"]: r for r in existing}
    new_runs = []
    updated = 0

    print(f"Polling {len(repos)} repos...")
    for repo in repos:
        for run in fetch_recent_runs(repo):
            rid = run["run_id"]
            if rid in by_id:
                # Refresh conclusion/status so runs first seen in progress
                # eventually report their real result (success/failure).
                stored = by_id[rid]
                if stored.get("conclusion") != run["conclusion"] or stored.get("status") != run["status"]:
                    stored["conclusion"] = run["conclusion"]
                    stored["status"] = run["status"]
                    updated += 1
            else:
                by_id[rid] = run
                new_runs.append(run)

    print(f"Found {len(new_runs)} new run(s), refreshed {updated} existing.")
    existing = sorted(by_id.values(), key=lambda r: (r.get("created_at") or "", r["repo"]))
    existing.extend(new_runs)
    existing.sort(key=lambda r: (r.get("created_at") or "", r["repo"]))
    save_log(existing)
    update_today(existing, repos)

    # ── today summary for ntfy ────────────────────────────────────────────
    today = now_pkt().strftime("%Y-%m-%d")
    todays = sorted(
        [r for r in existing if pkt_date(r.get("created_at", "")) == today],
        key=lambda r: r.get("created_at", ""),
    )
    total_failed = sum(1 for r in todays if is_failed(r))

    lines = [
        f"Repos tracked: {len(repos)}",
        f"New runs this poll: {len(new_runs)}",
        f"Total runs today: {len(todays)}" + (f" ({total_failed} failed)" if total_failed else ""),
        "",
        "Per repo:",
    ]
    by_repo: dict[str, list] = {}
    for run in todays:
        by_repo.setdefault(run["repo"], []).append(run)

    for repo in sorted(repos):
        short = repo.split("/")[-1]
        runs_in_repo = by_repo.get(repo, [])
        if not runs_in_repo:
            lines.append(f"- ⚪ {short}: not run today")
            continue
        fails = [r for r in runs_in_repo if is_failed(r)]
        last = runs_in_repo[-1]
        marker = "❌" if fails else "✅"
        line = (
            f"- {marker} {short}: {len(runs_in_repo)} run(s)"
            f" · last {pkt_time(last['created_at'])} PKT"
        )
        if fails:
            line += f" · {len(fails)} failed @ {', '.join(pkt_time(f['created_at']) for f in fails)}"
        lines.append(line)

    # ── failed runs detail ────────────────────────────────────────────────
    failed_todays = [r for r in todays if is_failed(r)]
    if failed_todays:
        lines += ["", "Failed:", ""]
        for r in failed_todays:
            short = r["repo"].split("/")[-1]
            label = r.get("workflow") or "unknown workflow"
            run_url = f"https://github.com/{r['repo']}/actions/runs/{r['run_id']}"
            lines.append(
                f"- ❌ {pkt_time(r['created_at'])} PKT · {short} · {label} · "
                f"{r.get('conclusion') or r.get('status') or '?'} · {run_url}"
            )

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
    title = f"Run Tracker · {today} · {len(todays)} run(s)" + (f" · {total_failed} failed" if total_failed else "")
    message = "\n".join(lines)
    if send_ntfy(title, message):
        print(f"ntfy sent to {NTFY_TOPIC}")
    else:
        print("ntfy send failed")


if __name__ == "__main__":
    main()
