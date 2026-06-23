# TSA NTC2026 Results Monitor

Checks the TSA National Conference 2026 results pages every ~10 minutes and emails
the configured recipient whenever a new **High School** event is posted —
reporting the result type (Tier 1 / Semifinalist), the event, and whether our
team or any tracked students appear.

- **Tier 1 Winners** ("Preliminary Top 24"): https://tsamembership.registermychapter.com/tier1winners/ntc2026
- **Semifinalists**: https://tsamembership.registermychapter.com/semifinalists/ntc2026

**It runs in the cloud, scheduled by [cron-job.org](https://cron-job.org) and
executed on [Render](https://render.com)** — see
[Hosting on Render + cron-job.org](#hosting-on-render--cron-joborg). This gives
low-latency checks (cron-job.org fires punctually, unlike GitHub's best-effort
scheduler). The GitHub Actions workflow is kept as a manual/fallback runner. Your
Mac does **not** need to be on. (It can also run locally on a Mac via launchd —
see [Running locally](#running-locally-optional).)

## What it reports per new HS event
- **Team events** (`T<id> Team #N`): whether our team **1211** is present, and as **Team #1 / #2 / …** (lists all if we field multiple teams).
- **Individual events** (numeric student IDs): which tracked students appear, by **name and ID**.
- HS events with no match are still reported ("not in this event"). Middle School (MS) events are ignored.

All new events found in one check are bundled into a single **HTML digest email**
(with a plain-text fallback): a header banner, a section per result type, and a
color-coded card per event (green when our team/students are present, gray when
not). The same content is sent as plain text for clients that need it.

## Hosting on Render + cron-job.org
This is the primary deployment. **Render** runs a tiny Flask wrapper
([`app.py`](app.py)) around `tsa_monitor.py`; **cron-job.org** pings it every
10 minutes. The script itself is unchanged.

- **Trigger:** cron-job.org sends an authenticated request to
  `https://<your-service>.onrender.com/run` every 10 min. Because that's more
  often than Render's 15-min free-tier idle timeout, the pings keep the service
  warm, so cold starts are rare.
- **Compute:** Render free web service, `gunicorn app:app --workers 1` (one
  worker so the in-process lock serializes runs). Config lives in
  [`render.yaml`](render.yaml).
- **State:** Render's disk is ephemeral, so `app.py` reads/writes `state.json`
  in **this repo on the `monitor-state` branch** via the GitHub Contents API.
  A side branch is used so state commits **don't** trigger Render redeploys
  (Render only auto-deploys `main`).
- **Security:** `/run` requires a secret `RUN_TOKEN` (sent as the `X-Run-Token`
  header). Render generates it; you copy it into cron-job.org.

### One-time setup
1. **Create the state branch** (empty `state.json` so the first run baselines
   silently):
   ```bash
   git checkout --orphan monitor-state
   git rm -rf .            # nothing tracked on this branch
   git commit --allow-empty -m "Init monitor-state branch"
   git push origin monitor-state
   git checkout main
   ```
   (You can leave it empty — `app.py` creates `state.json` on it on first run.)
2. **GitHub token:** create a fine-grained PAT with **Contents: read & write**
   on `prbhagam/tsa-monitor`. This becomes `GITHUB_TOKEN` on Render.
3. **Brevo (email):** Render blocks outbound SMTP, so mail is sent via Brevo's
   HTTPS API. In [Brevo](https://brevo.com): create a free account → **Senders &
   IPs** → verify your `SENDER_EMAIL` as a sender → **SMTP & API → API Keys** →
   create a key. That key becomes `BREVO_API_KEY` on Render. (`EMAIL_BACKEND` is
   already set to `brevo` in `render.yaml`.)
4. **Deploy to Render:** New → **Blueprint** → pick this repo (Render reads
   `render.yaml`). In the dashboard set the `sync:false` env vars:
   `SENDER_EMAIL`, `RECIPIENT_EMAIL`, `BREVO_API_KEY`, `GITHUB_TOKEN`.
   `RUN_TOKEN` is auto-generated — copy its value from the env tab.
5. **Verify:** visit `https://<service>.onrender.com/` (should return `ok`),
   then trigger one run manually:
   ```bash
   curl -H "X-Run-Token: <RUN_TOKEN>" https://<service>.onrender.com/run
   # -> "ok" (or "ok changed")   first run baselines silently, no email
   ```
6. **Schedule on cron-job.org:** create a job hitting
   `https://<service>.onrender.com/run` every 10 minutes, and under
   **Advanced → Headers** add `X-Run-Token: <RUN_TOKEN>`. Enable failure
   notifications so you hear about outages.

### Verifying email delivery
Before results are posted there's nothing to email, so use one of these:

- **Mail path (quick, non-destructive):** confirms Render can actually send via
  Brevo — the one thing local tests can't verify. Sends a test message to
  `RECIPIENT_EMAIL`; touches nothing else.
  ```bash
  curl -H "X-Run-Token: <RUN_TOKEN>" https://<service>.onrender.com/send-test-email
  # -> "ok sent"   then check your inbox
  ```
- **Full digest (end-to-end):** temporarily point a URL at last year's populated
  page so there are "new" events vs. the baseline, then trigger a run. In Render
  set `SEMIFINALIST_URL=https://tsamembership.registermychapter.com/semifinalists/ntc2025`,
  run `/run`, and you'll get a real formatted digest email. **Then revert the URL
  and re-baseline** so live 2026 state is clean:
  ```bash
  # after reverting SEMIFINALIST_URL, delete the polluted state so 2026 baselines fresh:
  curl -X DELETE -H "Authorization: Bearer <GITHUB_TOKEN>" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/prbhagam/tsa-monitor/contents/state.json" \
    -d "{\"message\":\"reset state\",\"branch\":\"monitor-state\",\"sha\":\"$(git fetch -q origin monitor-state && git rev-parse origin/monitor-state:state.json)\"}"
  ```

> **Don't run both schedulers.** The GitHub Actions `schedule` is commented out
> in `monitor.yml` precisely so Render+cron-job.org is the only active scheduler
> (otherwise you'd get duplicate emails, since each keeps its own state). To fall
> back to Actions, pause the cron-job.org job and uncomment the `schedule:` block.

## How it runs (GitHub Actions — fallback)
The workflow [`.github/workflows/monitor.yml`](.github/workflows/monitor.yml):

- Triggers on `workflow_dispatch` (the manual **Run workflow** button). The
  `schedule` cron is commented out (scheduling moved to cron-job.org); uncomment
  it to use Actions as the scheduler again.
- Installs deps, runs `python tsa_monitor.py`, then commits the updated
  `state.json` back to the repo so progress persists between runs
  (`permissions: contents: write`, commit tagged `[skip ci]`).
- A `concurrency` group prevents two checks from racing on `state.json`.

### Configuration & secrets
- **Secrets** (encrypted, in repo **Settings → Secrets and variables → Actions**):
  `SENDER_EMAIL`, `SENDER_APP_PASSWORD`, `RECIPIENT_EMAIL`, and `STUDENTS`. The
  roster is kept secret (not committed) because this repo is public — it holds
  student names and IDs. `STUDENTS` format is `id:Name,id:Name,...`. Set it with
  `gh secret set STUDENTS --repo <owner/repo>`.
- **Non-secret config** is the `env:` block in `monitor.yml`: `SMTP_HOST`,
  `SMTP_PORT`, `TIER1_URL`, `SEMIFINALIST_URL`, `TEAM_ID`, `DIVISIONS`. Edit and
  commit to change what's tracked. `DIVISIONS` is comma-separated (e.g. `HS`).

The script reads config from environment variables first, falling back to a local
`.env` only when present — so it needs no `.env` in CI.

### Controlling it
In the browser (**Actions** tab of the repo):
- **Run now:** "TSA results monitor" → **Run workflow**.
- **Pause / resume:** "TSA results monitor" → **⋯ → Disable / Enable workflow**.
- **History & logs:** click any run.

Or with the `gh` CLI:
```bash
gh run list   --repo prbhagam/tsa-monitor
gh run view   <run-id> --repo prbhagam/tsa-monitor --log
gh workflow run "TSA results monitor" --repo prbhagam/tsa-monitor
gh secret set SENDER_APP_PASSWORD --repo prbhagam/tsa-monitor   # reads value from stdin
```

### Caveats
- GitHub's scheduler is best-effort; runs can be delayed a few minutes under load.
- GitHub auto-disables a scheduled workflow after ~60 days with **no commits** to
  the repo. While results are posting, the `state.json` commits keep it active; in
  a long quiet period, just click **Enable workflow** once.

## How "new" is detected
`state.json` records a hash of each event's entry list per page. An event is
**new** if its name hasn't been seen, or **updated** if its entries changed after
first posting (so late additions aren't missed). The **first run ever** (no
`state.json`) saves a silent baseline and sends no email — but `state.json` is
committed in the repo as an empty baseline, so the cloud emails on the first
genuinely new event.

If an email fails to send, state is **not** saved, so the next run retries.

## Running locally (optional)
The repo also works as a standalone Mac/CLI tool. From a checkout:

```bash
cd ~/tsa-monitor
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
cp .env.example .env   # then fill in real values

./venv/bin/python tsa_monitor.py              # one check
./venv/bin/python tsa_monitor.py --dry-run    # parse + preview, no email/state; writes email_preview.html
./venv/bin/python tsa_monitor.py --test-email # send a test message to verify SMTP
./venv/bin/python tsa_monitor.py --reset      # delete saved state (re-baseline next run)
```

Preview against last year's populated page (parser sanity check):
```bash
SEMIFINALIST_URL="https://tsamembership.registermychapter.com/semifinalists/ntc2025" \
  ./venv/bin/python tsa_monitor.py --dry-run
```

### Scheduling on a Mac with launchd (instead of the cloud)
> Not used currently — the cloud handles scheduling. Running both at once causes
> duplicate emails (each keeps separate state). Keep the project **outside**
> `~/Documents`/`~/Desktop`/`~/Downloads`, which macOS Privacy/TCC blocks launchd
> from reading.

```bash
cp ~/tsa-monitor/com.southforsythtsa.tsamonitor.plist ~/Library/LaunchAgents/
launchctl bootout  gui/$(id -u)/com.southforsythtsa.tsamonitor 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.southforsythtsa.tsamonitor.plist

launchctl list | grep tsamonitor      # 2nd column = last exit code (0 = ok)

# stop / uninstall
launchctl bootout gui/$(id -u)/com.southforsythtsa.tsamonitor
rm ~/Library/LaunchAgents/com.southforsythtsa.tsamonitor.plist
```
The Mac must be on and awake; launchd runs a missed check when it wakes.

## Logs
- **Cloud:** the **Actions** tab — each run's "Run monitor" step shows the log.
- **Local:** `monitor.log` (app log); `launchd.out.log` / `launchd.err.log` if using launchd.

## Files
| File | Purpose |
|------|---------|
| `tsa_monitor.py` | Scraper + matcher + HTML/text emailer (run once per invocation) |
| `app.py` | Flask wrapper: `/run` endpoint for cron-job.org; syncs state to GitHub |
| `render.yaml` | Render Blueprint (free web service, env config) |
| `.github/workflows/monitor.yml` | GitHub Actions fallback runner (manual; schedule disabled) |
| `requirements.txt` | Python deps (requests, beautifulsoup4, python-dotenv, flask, gunicorn) |
| `state.json` | Seen-events state (tracked in git; persisted across cloud runs) |
| `.env` | Local-only config/secrets (git-ignored; not used in CI) |
| `.env.example` | Template for `.env` |
| `run.sh` | launchd wrapper for local Mac scheduling (uses the venv) |
| `com.southforsythtsa.tsamonitor.plist` | LaunchAgent definition for local Mac scheduling |
