# Run Tracker

Tracks how many times your other GitHub repositories' workflows run each day and at what times.

This tracker repo does **not** count its own runs. It polls the GitHub Actions API for the repos listed in `repos.json`, keeps detailed run history and the daily report in ignored local `private-data/`, and sends an **ntfy** notification after every poll. Keep this repository list limited to confirmed public repositories because the list itself is public.

All timestamps are in **Pakistan Standard Time (PKT, UTC+5)**.

## How it works

- The workflow is triggered by a **`repository_dispatch`** event of type `run-tracker-poll` (and can also be run manually).
- `poll_runs.py` queries each repo's `actions/runs` endpoint, appends new runs to `private-data/run-log.json`, rewrites `private-data/TODAY.md`, and sends an ntfy notification.

## ntfy notifications

After every poll, a notification is posted to your configured ntfy topic, showing:

- repos tracked
- new runs this poll
- total runs today
- per-repo breakdown: run count + last run time (PKT)

The topic is set via the **`NTFY_TOPIC`** repository secret (not stored in code).

## Auto-discovery on the last run of the day

On the last run of the day, `poll_runs.py` lists public repos in your account and appends any new public repos to `repos.json`. Private repos are never added to this public file.

- **cron-job.org**: set `"last_run_of_day": true` in the dispatch body on your final job of the day.
- **Fallback**: if the run happens at PKT hour ≥ 23 (configurable via `LAST_RUN_HOUR`), discovery runs automatically.

## Setup

### Secrets

| Secret | Purpose |
|--------|---------|
| `REPO_ACCESS_TOKEN` | PAT with `repo` scope for cross-repo API access |
| `NTFY_TOPIC` | ntfy.sh topic name for notifications |

### cron-job.org

1. Create a PAT with `repo` scope and add it as `REPO_ACCESS_TOKEN`.
2. Create the job:
   - **URL**: `https://api.github.com/repos/<owner>/run-tracker/dispatches`
   - **Method**: `POST`
   - **Headers**: `Accept: application/vnd.github+json`, `Authorization: Bearer <PAT>`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
   - **Body**: `{"event_type":"run-tracker-poll"}`
   - **Schedule**: your preferred cron
3. For the **last run of the day**, use body:
   `{"event_type":"run-tracker-poll","client_payload":{"last_run_of_day":true}}`

## Files

- `repos.json` — list of owner/repo targets to track
- `poll_runs.py` — polling script (run locally with `gh` authenticated)
- `private-data/run-log.json` — local full run history (ignored by Git)
- `private-data/TODAY.md` — local daily report (ignored by Git)

The GitHub Actions workflow does not persist detailed run history or reports; its filesystem is temporary. For durable private history, set `RUN_TRACKER_PRIVATE_DIR` to a private directory on a trusted machine and run the tracker there. Public Actions logs may still show tracker job timing.

## Local usage

```bash
gh auth login
NTFY_TOPIC=<your-topic> python poll_runs.py
```
